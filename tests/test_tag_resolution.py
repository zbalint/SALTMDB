import unittest
import tempfile
import os
import shutil
from saltmdb.db.schema import init_db
from saltmdb.db.connection import write_transaction_retrying
from saltmdb.domain.services.memory_service import resolve_or_create_tag, normalize_tag_name
from saltmdb.domain.services import event_service


class TestNormalizeTagName(unittest.TestCase):
    def test_bare_name_gets_hash_prefixed(self):
        self.assertEqual(normalize_tag_name("bugfix"), "#bugfix")

    def test_already_prefixed_name_is_unchanged(self):
        self.assertEqual(normalize_tag_name("#bugfix"), "#bugfix")

    def test_empty_or_none_returns_empty_string(self):
        self.assertEqual(normalize_tag_name(""), "")
        self.assertEqual(normalize_tag_name(None), "")


class TestResolveOrCreateTag(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _resolve(self, tag_name, agent_id=None):
        # resolve_or_create_tag's contract requires an open write transaction around it
        # (mirrors how store_memory/commit_consolidation actually invoke it).
        def _write(c):
            return resolve_or_create_tag(c, tag_name, agent_id=agent_id)
        return write_transaction_retrying(self.conn, _write)

    def test_exact_match_resolves_to_same_id_on_repeated_calls(self):
        id1 = self._resolve("#exactmatch")
        id2 = self._resolve("#exactmatch")
        self.assertIsNotNone(id1)
        self.assertEqual(id1, id2)

    def test_normalized_punctuation_variant_resolves_to_same_id(self):
        id1 = self._resolve("#bugfix")
        id2 = self._resolve("Bug-Fix")
        self.assertIsNotNone(id1)
        self.assertEqual(id1, id2, "'Bug-Fix' should normalize onto the same row as '#bugfix'")

    def test_plural_suffix_fallback_resolves_to_same_id(self):
        id1 = self._resolve("#skill")
        id2 = self._resolve("#skills")
        self.assertIsNotNone(id1)
        self.assertEqual(id1, id2, "'#skills' should resolve onto '#skill' via the plural/suffix fallback")

        # Confirm only one physical tag row was ever created for this family -- the whole
        # point of the fallback is that they never fragment into two rows in the first place.
        rows = self.conn.execute(
            "SELECT id FROM tags WHERE lower(name) IN ('#skill', '#skills')"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_alias_resolution_returns_canonical_id_not_alias_id(self):
        canonical_id = self._resolve("#canonicaltag")
        alias_id = self._resolve("#aliastag")
        self.assertNotEqual(canonical_id, alias_id)

        # Simulate a prior merge_tags() call by manually pointing the alias's canonical_id
        # at the canonical tag's id.
        def _write(c):
            c.execute(
                "UPDATE tags SET canonical_id = ? WHERE id = ?", (canonical_id, alias_id)
            )
        write_transaction_retrying(self.conn, _write)

        resolved = self._resolve("#aliastag")
        self.assertEqual(
            resolved, canonical_id,
            "resolve_or_create_tag must follow canonical_id and return the CANONICAL id, not the alias's own id"
        )
        self.assertNotEqual(resolved, alias_id)

    def test_shape_sanitization_sanitizes_and_logs_exactly_one_issue_event(self):
        malformed = "#Bad Tag!! Name"
        tag_id = self._resolve(malformed, agent_id="tester")
        self.assertIsNotNone(tag_id)

        row = self.conn.execute("SELECT name FROM tags WHERE id = ?", (tag_id,)).fetchone()
        self.assertIsNotNone(row)
        # Sanitized: lowercase, only [a-z0-9-] retained after the leading '#'.
        self.assertRegex(row[0], r'^#[a-z0-9-]+$')

        events = event_service.get_recent_events(type_filter="issue", db_connection=self.conn)
        matching = [e for e in events if "sanitiz" in e["content"].lower()]
        self.assertEqual(
            len(matching), 1,
            "sanitizing a malformed tag name must fire exactly one 'issue' event, not zero or many"
        )

    def test_new_tag_creation_populates_normalized_name(self):
        tag_id = self._resolve("#freshnewtag")
        row = self.conn.execute(
            "SELECT normalized_name FROM tags WHERE id = ?", (tag_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0], "normalized_name must be populated for newly created tag rows")
        self.assertEqual(row[0], "freshnewtag")


if __name__ == "__main__":
    unittest.main()
