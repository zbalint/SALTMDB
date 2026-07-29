import unittest
import tempfile
import os
import shutil
from saltmdb.db.schema import init_db
from saltmdb.mcp import tools
from saltmdb.domain.services import librarian_service

class TestTagMergeTool(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _tag_names_for_entity(self, entity_id):
        rows = self.conn.execute("""
            SELECT t.name FROM entity_tags et JOIN tags t ON et.tag_id = t.id WHERE et.entity_id = ?
        """, (entity_id,)).fetchall()
        return sorted(r[0] for r in rows)

    def test_merge_tags_repoints_entity_tags_to_canonical(self):
        res1 = tools.store_memory(content="Content for entity tagged with the fix fragment", title="Fix Fragment Entity", tags=["#fix"], owner_id="user1", skip_duplicate_check=True)
        id1 = res1.split("ID: ")[1].split()[0]

        res2 = tools.store_memory(content="Content for entity tagged with the bugfix fragment", title="Bugfix Fragment Entity", tags=["#bugfix"], owner_id="user1", skip_duplicate_check=True)
        id2 = res2.split("ID: ")[1].split()[0]

        merge_res = tools.merge_tags(keep_tag="#fix", tags_to_merge=["#bugfix"])
        self.assertIn("Merged 1 tag(s)", merge_res)

        self.assertEqual(self._tag_names_for_entity(id1), ["#fix"])
        self.assertEqual(self._tag_names_for_entity(id2), ["#fix"])

        remaining = self.conn.execute("SELECT name, canonical_id FROM tags WHERE lower(name) = '#bugfix'").fetchone()
        self.assertIsNotNone(remaining[1], "merged tag should have canonical_id set, not deleted")

    def test_merge_tags_idempotent_when_run_twice(self):
        # NOTE: previously used #skill/#skills as the fixture pair, but those are now
        # resolved onto the SAME tag row automatically at write time by
        # resolve_or_create_tag()'s plural/suffix fallback (see
        # test_plural_suffix_tags_auto_resolve_to_same_tag_at_write_time below) -- so they
        # never land as two rows needing an explicit merge in the first place. #docs and
        # #documentation are genuinely different-looking strings (not a simple '-s' suffix
        # of each other), so they still land as two separate rows and this test continues
        # to exercise a real, explicit merge_tags() call and its idempotency.
        res1 = tools.store_memory(content="Content for entity tagged with the docs fragment", title="Docs Fragment Entity", tags=["#docs", "#documentation"], owner_id="user1", skip_duplicate_check=True)
        id1 = res1.split("ID: ")[1].split()[0]

        first = tools.merge_tags(keep_tag="#documentation", tags_to_merge=["#docs"])
        self.assertIn("Merged 1 tag(s)", first)
        self.assertEqual(self._tag_names_for_entity(id1), ["#documentation"])

        second = tools.merge_tags(keep_tag="#documentation", tags_to_merge=["#docs"])
        self.assertIn("Skipped", second)
        self.assertEqual(self._tag_names_for_entity(id1), ["#documentation"])

    def test_plural_suffix_tags_auto_resolve_to_same_tag_at_write_time(self):
        """New behavior: resolve_or_create_tag()'s plural/suffix fallback means #skill and
        #skills now resolve to the SAME tag row automatically at write time, so they never
        fragment into two rows needing a later merge_tags() call."""
        res1 = tools.store_memory(content="Content for entity tagged with skill singular", title="Skill Singular Entity", tags=["#skill"], owner_id="user1", skip_duplicate_check=True)
        id1 = res1.split("ID: ")[1].split()[0]

        res2 = tools.store_memory(content="Content for entity tagged with skills plural", title="Skills Plural Entity", tags=["#skills"], owner_id="user1", skip_duplicate_check=True)
        id2 = res2.split("ID: ")[1].split()[0]

        tags1 = self._tag_names_for_entity(id1)
        tags2 = self._tag_names_for_entity(id2)
        self.assertEqual(len(tags1), 1)
        self.assertEqual(tags1, tags2, "#skill and #skills should resolve to the same underlying tag row")

        rows = self.conn.execute(
            "SELECT id FROM tags WHERE lower(name) IN ('#skill', '#skills')"
        ).fetchall()
        self.assertEqual(
            len(rows), 1,
            "the plural fallback should prevent a second row from ever being created for this pair"
        )

    def test_merge_tags_missing_keep_tag_errors(self):
        res = librarian_service.merge_tags(keep_tag="#does-not-exist", tags_to_merge=["#fix"], db_path=self.db_path)
        self.assertIn("Error", res)

    def test_merge_tags_missing_alias_is_skipped_not_fatal(self):
        tools.store_memory(content="Content for entity tagged with docs canonical tag", title="Docs Canonical Entity", tags=["#docs"], owner_id="user1", skip_duplicate_check=True)
        res = tools.merge_tags(keep_tag="#docs", tags_to_merge=["#nonexistent-alias"])
        self.assertIn("Merged 0 tag(s)", res)
        self.assertIn("not found", res)

if __name__ == "__main__":
    unittest.main()
