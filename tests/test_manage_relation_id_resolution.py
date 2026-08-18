import os
import shutil
import tempfile
import unittest
import uuid as uuid_mod

from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service, relation_service


def _store(conn, title, content=None, **kw):
    """Store a memory and return its real UUID (never hardcode IDs, per repo convention)."""
    if content is None:
        content = f"{title}\n\nBody content for the test memory."
    res = memory_service.store_memory(
        content=content,
        title=title,
        owner_id="owner_a",
        skip_duplicate_check=True,
        db_connection=conn,
        **kw,
    )
    return res.split("ID: ")[1].strip()


class TestManageRelationIdResolution(unittest.TestCase):
    """§4.4: manage_relation and consolidation parent resolution now fall back to short
    hex-prefix resolution (resolve_id_prefix) instead of leaking a raw FOREIGN KEY constraint
    failure (§3.15) when given an id that resolve_entity_id alone can't place."""

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

    # -- store_relation --------------------------------------------------

    def test_store_relation_resolves_short_prefix_on_both_ends(self):
        source_id = _store(self.conn, "Prefix Source")
        target_id = _store(self.conn, "Prefix Target")
        result = relation_service.store_relation(
            source_id=source_id[:8],
            target_id=target_id[:8],
            predicate="related_to",
            db_connection=self.conn,
        )
        self.assertIn("successfully stored", result)
        row = self.conn.execute(
            "SELECT source_id, target_id FROM relations WHERE predicate = 'related_to'"
        ).fetchone()
        self.assertEqual(row, (source_id, target_id))

    def test_store_relation_unknown_id_gives_named_error_not_fk_crash(self):
        target_id = _store(self.conn, "Real Target")
        result = relation_service.store_relation(
            source_id="deadbeef00000000",  # well-formed hex, matches nothing
            target_id=target_id,
            predicate="related_to",
            db_connection=self.conn,
        )
        self.assertIn("UNKNOWN_ENTITY_ID", result)
        self.assertNotIn("FOREIGN KEY", result)

    def test_store_relation_ambiguous_prefix_lists_candidates(self):
        # Two entities sharing an 8-char prefix collide deliberately by forcing the id.
        id_a = _store(self.conn, "Ambiguous A")
        shared_prefix = id_a[:8]
        # Craft a second, otherwise-unrelated UUID that starts with the same 8 hex chars.
        id_b = shared_prefix + str(uuid_mod.uuid4())[8:]
        self.conn.execute(
            "INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, "
            "title, full_content, status) VALUES (?, datetime('now'), datetime('now'), "
            "datetime('now'), 'owner_a', 'Ambiguous B', 'body text here', 'raw')",
            (id_b,),
        )
        target_id = _store(self.conn, "Some Target")
        result = relation_service.store_relation(
            source_id=shared_prefix,
            target_id=target_id,
            predicate="related_to",
            db_connection=self.conn,
        )
        self.assertIn("AMBIGUOUS_ID_PREFIX", result)
        self.assertIn(id_a, result)
        self.assertIn(id_b, result)

    # -- invalidate_relation ----------------------------------------------

    def test_invalidate_relation_resolves_short_prefix(self):
        source_id = _store(self.conn, "Invalidate Source")
        target_id = _store(self.conn, "Invalidate Target")
        relation_service.store_relation(
            source_id=source_id,
            target_id=target_id,
            predicate="related_to",
            db_connection=self.conn,
        )
        result = relation_service.invalidate_relation(
            source_id=source_id[:8],
            target_id=target_id[:8],
            predicate="related_to",
            db_connection=self.conn,
        )
        self.assertIn("invalidated", result)

    def test_invalidate_relation_unknown_id_gives_named_error(self):
        target_id = _store(self.conn, "Invalidate Target Only")
        result = relation_service.invalidate_relation(
            source_id="cafebabe00000000",
            target_id=target_id,
            predicate="related_to",
            db_connection=self.conn,
        )
        self.assertIn("UNKNOWN_ENTITY_ID", result)
        self.assertNotIn("FOREIGN KEY", result)

    # -- consolidation parent resolution ------------------------------------

    def test_consolidation_parent_resolution_accepts_short_prefix(self):
        parent_a = _store(self.conn, "Consolidation Parent A")
        parent_b = _store(self.conn, "Consolidation Parent B")
        resolved = relation_service._resolve_and_filter_parent_ids(
            self.conn, [parent_a[:8], parent_b[:8]]
        )
        self.assertEqual(set(resolved), {parent_a, parent_b})

    def test_consolidation_parent_resolution_drops_unresolvable(self):
        parent_a = _store(self.conn, "Consolidation Parent Only")
        resolved = relation_service._resolve_and_filter_parent_ids(
            self.conn, [parent_a, "deadbeef00000000"]
        )
        self.assertEqual(resolved, [parent_a])


if __name__ == "__main__":
    unittest.main()
