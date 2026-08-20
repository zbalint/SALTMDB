import os
import shutil
import tempfile
import unittest

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import revise_memory, store_memory, supersede_memory


class TestImmutableLifecycleReplacements(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.conn = init_db(os.path.join(self.temp_dir, "lifecycle.db"))

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store(self, title="Original lifecycle memory", tags=None):
        result = store_memory(
            title=title,
            content=f"A complete and sufficiently descriptive lifecycle memory body for {title}.",
            tags=tags or ["#original"],
            owner_id="lifecycle-tests",
            context_id="lifecycle-context",
            memory_type="decision",
            db_connection=self.conn,
        )
        self.assertEqual(result["status"], "ok")
        return result["data"]["id"]

    def _tags(self, entity_id):
        return self.conn.execute(
            """
            SELECT t.name FROM tags t JOIN entity_tags et ON et.tag_id = t.id
            WHERE et.entity_id = ? ORDER BY t.name
            """,
            (entity_id,),
        ).fetchall()

    def test_revise_preserves_frozen_predecessor_and_creates_new_id(self):
        old_id = self._store()
        frozen_columns = (
            "title, full_content, owner_id, context_id, scope, memory_type, created_at, "
            "content_hash, metadata, parent_ids, valid_from"
        )
        before = self.conn.execute(
            f"SELECT {frozen_columns} FROM entities WHERE id = ?", (old_id,)
        ).fetchone()
        old_tags = self._tags(old_id)

        result = revise_memory(
            entity_id=old_id,
            title="Corrected lifecycle memory",
            tags=["#corrected"],
            content="A complete and sufficiently descriptive corrected lifecycle body.",
            reason="The original representation was incomplete.",
            db_connection=self.conn,
        )

        self.assertEqual(result["status"], "ok")
        new_id = result["data"]["new_id"]
        self.assertNotEqual(new_id, old_id)
        after = self.conn.execute(
            f"SELECT {frozen_columns} FROM entities WHERE id = ?", (old_id,)
        ).fetchone()
        self.assertEqual(before, after)
        self.assertEqual(old_tags, self._tags(old_id))
        self.assertEqual(
            self.conn.execute("SELECT status FROM entities WHERE id = ?", (old_id,)).fetchone()[0],
            "archived",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT source_id, target_id, predicate FROM relations WHERE source_id = ?",
                (new_id,),
            ).fetchone(),
            (new_id, old_id, "revises"),
        )
        self.assertEqual(
            result["data"]["inherited_fields"], ["owner_id", "context_id", "scope", "memory_type"]
        )

    def test_supersede_does_not_repoint_semantic_relation(self):
        old_id = self._store()
        neighbor_id = self._store("Semantic neighbor", ["#neighbor"])
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at) "
            "VALUES ('semantic-edge', ?, ?, 'depends_on', datetime('now'), datetime('now'), datetime('now'))",
            (old_id, neighbor_id),
        )
        self.conn.commit()

        result = supersede_memory(
            entity_id=old_id,
            title="Newer lifecycle memory",
            tags=["#newer"],
            content="A complete and sufficiently descriptive newer lifecycle body.",
            reason="A later decision replaced the old one.",
            db_connection=self.conn,
        )

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["data"]["semantic_relations_repointed"])
        edge = self.conn.execute(
            "SELECT source_id, target_id, predicate, valid_to FROM relations WHERE id = 'semantic-edge'"
        ).fetchone()
        self.assertEqual(edge, (old_id, neighbor_id, "depends_on", None))
        self.assertEqual(
            result["data"]["orphaned_semantic_edges"][0]["relation_id"], "semantic-edge"
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM relations WHERE source_id = ? AND predicate = 'depends_on'",
                (result["data"]["new_id"],),
            ).fetchone()[0],
            0,
        )

    def test_inactive_target_rejects_with_successor_and_zero_side_effects(self):
        old_id = self._store()
        first = revise_memory(
            entity_id=old_id,
            title="First lifecycle successor",
            tags=["#first"],
            content="A complete and sufficiently descriptive first successor body.",
            reason="First correction.",
            db_connection=self.conn,
        )
        successor_id = first["data"]["new_id"]
        counts_before = self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM entities), (SELECT COUNT(*) FROM relations), "
            "(SELECT COUNT(*) FROM entity_tags)"
        ).fetchone()

        rejected = supersede_memory(
            entity_id=old_id,
            title="Should not be written",
            tags=["#bad"],
            content="This replacement must not be persisted.",
            reason="The target is inactive.",
            db_connection=self.conn,
        )

        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["errors"][0]["code"], "INACTIVE_TARGET")
        self.assertIn(
            successor_id, {item["id"] for item in rejected["effective"]["active_successors"]}
        )
        self.assertEqual(
            counts_before,
            self.conn.execute(
                "SELECT (SELECT COUNT(*) FROM entities), (SELECT COUNT(*) FROM relations), "
                "(SELECT COUNT(*) FROM entity_tags)"
            ).fetchone(),
        )


    def test_inactive_target_message_includes_corrects_guidance(self):
        old_id = self._store()
        revise_memory(
            entity_id=old_id,
            title="First lifecycle successor",
            tags=["#first"],
            content="A complete and sufficiently descriptive first successor body.",
            reason="First correction.",
            db_connection=self.conn,
        )

        rejected = supersede_memory(
            entity_id=old_id,
            title="Should not be written",
            tags=["#bad"],
            content="This replacement must not be persisted.",
            reason="The target is inactive.",
            db_connection=self.conn,
        )

        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["errors"][0]["code"], "INACTIVE_TARGET")
        self.assertIn("predicate='corrects'", rejected["errors"][0]["message"])


if __name__ == "__main__":
    unittest.main()
