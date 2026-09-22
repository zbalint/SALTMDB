import os
import shutil
import tempfile
import unittest
import uuid
from unittest.mock import patch

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

    def test_supersede_with_repoint_relations_repoints_both_directions(self):
        old_id = self._store()
        neighbor_out = self._store("Outbound neighbor", ["#out"])
        neighbor_in = self._store("Inbound neighbor", ["#in"])
        # old_id is the source of one edge and the target of another, so a correct
        # implementation must repoint whichever endpoint the predecessor occupies.
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at) "
            "VALUES ('edge-out', ?, ?, 'depends_on', datetime('now'), datetime('now'), datetime('now'))",
            (old_id, neighbor_out),
        )
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at) "
            "VALUES ('edge-in', ?, ?, 'elaborates_on', datetime('now'), datetime('now'), datetime('now'))",
            (neighbor_in, old_id),
        )
        self.conn.commit()

        result = supersede_memory(
            entity_id=old_id,
            title="Newer lifecycle memory",
            tags=["#newer"],
            content="A complete and sufficiently descriptive newer lifecycle body.",
            reason="A later decision replaced the old one.",
            repoint_relations=True,
            db_connection=self.conn,
        )

        self.assertEqual(result["status"], "ok")
        new_id = result["data"]["new_id"]
        self.assertTrue(result["data"]["semantic_relations_repointed"])
        repointed = {
            (r["source_id"], r["target_id"], r["predicate"])
            for r in result["data"]["repointed_relations"]
        }
        self.assertEqual(
            repointed,
            {(new_id, neighbor_out, "depends_on"), (neighbor_in, new_id, "elaborates_on")},
        )

        # Old edges are invalidated (history preserved), not deleted.
        for old_relation_id in ("edge-out", "edge-in"):
            valid_to, invalid_at = self.conn.execute(
                "SELECT valid_to, invalid_at FROM relations WHERE id = ?", (old_relation_id,)
            ).fetchone()
            self.assertIsNotNone(valid_to)
            self.assertIsNotNone(invalid_at)

        # New edges are active and correctly repointed onto the new entity.
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM relations WHERE source_id = ? AND target_id = ? "
                "AND predicate = 'depends_on' AND valid_to IS NULL",
                (new_id, neighbor_out),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM relations WHERE source_id = ? AND target_id = ? "
                "AND predicate = 'elaborates_on' AND valid_to IS NULL",
                (neighbor_in, new_id),
            ).fetchone()[0],
            1,
        )

        # No active edge is left referencing the archived predecessor.
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM relations WHERE (source_id = ? OR target_id = ?) "
                "AND predicate NOT IN ('revises', 'supersedes', 'consolidated_from') "
                "AND valid_to IS NULL",
                (old_id, old_id),
            ).fetchone()[0],
            0,
        )

    def test_revise_with_repoint_relations_thread_through(self):
        old_id = self._store()
        neighbor_id = self._store("Neighbor", ["#neighbor"])
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at) "
            "VALUES ('edge', ?, ?, 'related_to', datetime('now'), datetime('now'), datetime('now'))",
            (old_id, neighbor_id),
        )
        self.conn.commit()

        result = revise_memory(
            entity_id=old_id,
            title="Corrected lifecycle memory",
            tags=["#corrected"],
            content="A complete and sufficiently descriptive corrected lifecycle body.",
            reason="The original representation was incomplete.",
            repoint_relations=True,
            db_connection=self.conn,
        )

        self.assertEqual(result["status"], "ok")
        new_id = result["data"]["new_id"]
        self.assertTrue(result["data"]["semantic_relations_repointed"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM relations WHERE source_id = ? AND target_id = ? "
                "AND predicate = 'related_to' AND valid_to IS NULL",
                (new_id, neighbor_id),
            ).fetchone()[0],
            1,
        )

    def test_supersede_with_repoint_relations_leaves_self_loop_edge_orphaned(self):
        old_id = self._store()
        # A self-loop on the predecessor (a known historical anomaly, not creatable through the
        # normal relation_service path) -- repointing both endpoints to the same new entity
        # would produce a self-referential edge, which store_relation's own guard forbids. This
        # edge must be left completely untouched rather than silently dropped or fabricated.
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at) "
            "VALUES ('self-loop', ?, ?, 'related_to', datetime('now'), datetime('now'), datetime('now'))",
            (old_id, old_id),
        )
        self.conn.commit()

        result = supersede_memory(
            entity_id=old_id,
            title="Newer lifecycle memory",
            tags=["#newer"],
            content="A complete and sufficiently descriptive newer lifecycle body.",
            reason="A later decision replaced the old one.",
            repoint_relations=True,
            db_connection=self.conn,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["repointed_relations"], [])
        self.assertEqual(
            [e["relation_id"] for e in result["data"]["orphaned_semantic_edges"]], ["self-loop"]
        )
        row = self.conn.execute(
            "SELECT source_id, target_id, valid_to FROM relations WHERE id = 'self-loop'"
        ).fetchone()
        self.assertEqual(row, (old_id, old_id, None))

    def test_supersede_with_repoint_relations_skips_conflicting_duplicate_insert(self):
        old_id = self._store()
        neighbor_id = self._store("Neighbor", ["#neighbor"])
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at) "
            "VALUES ('edge', ?, ?, 'depends_on', datetime('now'), datetime('now'), datetime('now'))",
            (old_id, neighbor_id),
        )
        # An edge already active at the exact triple the repoint will produce -- forces the
        # INSERT ... ON CONFLICT DO NOTHING branch to actually skip, so the fix (don't report a
        # relation id that was never written) is genuinely exercised rather than merely unreachable.
        fixed_new_id = "11111111-1111-1111-1111-111111111111"
        # fixed_new_id doesn't exist in entities yet (supersede_memory below is what creates it),
        # so the FK constraint on relations.source_id must be relaxed for this one setup insert;
        # the reference becomes valid the moment supersede_memory inserts that entity row.
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at) "
            "VALUES ('pre-existing', ?, ?, 'depends_on', datetime('now'), datetime('now'), datetime('now'))",
            (fixed_new_id, neighbor_id),
        )
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.commit()

        real_uuid4 = uuid.uuid4
        call_count = {"n": 0}

        def _fake_uuid4():
            call_count["n"] += 1
            # new_id is the first uuid4() drawn inside the write transaction; forcing it to a
            # known value lets the pre-seeded conflicting edge collide deterministically.
            # relation_id and any per-edge new_relation_id draws stay real/random.
            return uuid.UUID(fixed_new_id) if call_count["n"] == 1 else real_uuid4()

        with patch(
            "saltmdb.domain.services.memory_service.lifecycle.uuid.uuid4",
            side_effect=_fake_uuid4,
        ):
            result = supersede_memory(
                entity_id=old_id,
                title="Newer lifecycle memory",
                tags=["#newer"],
                content="A complete and sufficiently descriptive newer lifecycle body.",
                reason="A later decision replaced the old one.",
                repoint_relations=True,
                db_connection=self.conn,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["new_id"], fixed_new_id)
        # The old edge is invalidated regardless of the insert collision.
        valid_to = self.conn.execute("SELECT valid_to FROM relations WHERE id = 'edge'").fetchone()[
            0
        ]
        self.assertIsNotNone(valid_to)
        # No relation id is reported for the skipped insert -- the pre-existing edge already
        # covers the triple, and repointed_relations must never name a row that doesn't exist.
        self.assertEqual(result["data"]["repointed_relations"], [])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM relations WHERE source_id = ? AND target_id = ? "
                "AND predicate = 'depends_on' AND valid_to IS NULL",
                (fixed_new_id, neighbor_id),
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
