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
        id1 = res1.split("ID: ")[1].strip()

        res2 = tools.store_memory(content="Content for entity tagged with the bugfix fragment", title="Bugfix Fragment Entity", tags=["#bugfix"], owner_id="user1", skip_duplicate_check=True)
        id2 = res2.split("ID: ")[1].strip()

        merge_res = tools.merge_tags(keep_tag="#fix", tags_to_merge=["#bugfix"])
        self.assertIn("Merged 1 tag(s)", merge_res)

        self.assertEqual(self._tag_names_for_entity(id1), ["#fix"])
        self.assertEqual(self._tag_names_for_entity(id2), ["#fix"])

        remaining = self.conn.execute("SELECT name, canonical_id FROM tags WHERE lower(name) = '#bugfix'").fetchone()
        self.assertIsNotNone(remaining[1], "merged tag should have canonical_id set, not deleted")

    def test_merge_tags_idempotent_when_run_twice(self):
        res1 = tools.store_memory(content="Content for entity tagged with the skills fragment", title="Skills Fragment Entity", tags=["#skill", "#skills"], owner_id="user1", skip_duplicate_check=True)
        id1 = res1.split("ID: ")[1].strip()

        first = tools.merge_tags(keep_tag="#skills", tags_to_merge=["#skill"])
        self.assertIn("Merged 1 tag(s)", first)
        self.assertEqual(self._tag_names_for_entity(id1), ["#skills"])

        second = tools.merge_tags(keep_tag="#skills", tags_to_merge=["#skill"])
        self.assertIn("Skipped", second)
        self.assertEqual(self._tag_names_for_entity(id1), ["#skills"])

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
