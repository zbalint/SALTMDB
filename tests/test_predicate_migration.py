"""Phase 8 data migration tests (agent API redesign plan §7): predicate-vocabulary rewrite
(§7.1), canonical registry rebuild (§7.2), and SCD-history revises backfill (§7.3), all shipped
as a `user_version = 2` block inside `init_db()` (§7.0 -- never a hand-run script).

These tests never touch a live database. Each one simulates a pre-Phase-6/pre-Phase-8 DB state
by initializing a fresh temp DB (which lands at the current user_version), manually resetting
`PRAGMA user_version` back down and hand-planting legacy rows the way an old, pre-redesign
daemon would have left them, then re-opening the SAME file via `init_db()` again to trigger the
migration for real -- exercising the actual production code path, not a reimplementation of it.
"""

import os
import shutil
import tempfile
import unittest
import uuid
from datetime import UTC, datetime

from saltmdb.db.schema import init_db


class _MigrationFixture(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "legacy.db")
        self.conn = init_db(self.db_path)  # lands at the current (post-migration) user_version

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _reset_to_pre_migration_state(self):
        """Rolls PRAGMA user_version back to 1 (post Track-A, pre Phase-8) and restores the
        pre-Phase-6 registry alias target (relates_to/references -> elaborates_on), simulating
        what a real live DB from before this redesign would look like."""
        self.conn.execute("PRAGMA user_version = 1")
        elab_id = self.conn.execute(
            "SELECT id FROM predicates WHERE name = 'elaborates_on'"
        ).fetchone()[0]
        self.conn.execute(
            "UPDATE predicates SET canonical_id = ? WHERE name IN ('relates_to', 'references')",
            (elab_id,),
        )

    def _mk_entity(self, title, *, status="raw"):
        entity_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, "
            "scope, status, title, full_content, valid_from) VALUES "
            "(?, ?, ?, ?, 'tester', 'shared', ?, ?, 'body body body body', ?)",
            (entity_id, now, now, now, status, title, now),
        )
        return entity_id

    def _mk_relation(self, source_id, target_id, predicate, *, valid_to=None):
        rel_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, "
            "valid_from, valid_to) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rel_id, source_id, target_id, predicate, now, now, valid_to),
        )
        return rel_id

    def _reopen(self):
        """Closes the current connection and re-opens the same file via init_db(), the same
        way a real daemon restart would -- this is what actually triggers the migration."""
        self.conn.close()
        self.conn = init_db(self.db_path)

    def _row(self, rel_id):
        return self.conn.execute(
            "SELECT source_id, target_id, predicate, valid_to FROM relations WHERE id = ?",
            (rel_id,),
        ).fetchone()


class TestPredicateDriftMigration(_MigrationFixture):
    """§7.1: rewriting drifted relation edges onto their canonical spelling."""

    def test_same_direction_alias_renamed_no_collision(self):
        a, b = self._mk_entity("A"), self._mk_entity("B")
        self._reset_to_pre_migration_state()
        r1 = self._mk_relation(a, b, "relates_to")
        self.conn.commit()
        self._reopen()
        self.assertEqual(self._row(r1), (a, b, "related_to", None))

    def test_swap_alias_renamed_and_direction_exchanged(self):
        c, d = self._mk_entity("C"), self._mk_entity("D")
        self._reset_to_pre_migration_state()
        r2 = self._mk_relation(c, d, "resolved_by")  # C resolved_by D -> D resolves C
        self.conn.commit()
        self._reopen()
        self.assertEqual(self._row(r2), (d, c, "resolves", None))

    def test_alias_vs_alias_collision_keeps_earlier_row_active_and_closes_later_one(self):
        a, b = self._mk_entity("A"), self._mk_entity("B")
        self._reset_to_pre_migration_state()
        winner = self._mk_relation(a, b, "relates_to")  # -> related_to, inserted first
        loser = self._mk_relation(a, b, "references")  # -> related_to, same pair, collides
        self.conn.commit()
        self._reopen()

        self.assertEqual(self._row(winner), (a, b, "related_to", None))
        loser_row = self._row(loser)
        self.assertEqual(loser_row[:3], (a, b, "related_to"))
        self.assertIsNotNone(loser_row[3], "the collision loser must be closed, not deleted")

    def test_alias_vs_already_canonical_collision_keeps_canonical_and_closes_alias(self):
        d, e = self._mk_entity("D"), self._mk_entity("E")
        self._reset_to_pre_migration_state()
        canonical = self._mk_relation(d, e, "resolves")  # already canonical, untouched
        # E remediated_by D -> D resolves E -- collides with the existing canonical row above.
        alias = self._mk_relation(e, d, "remediated_by")
        self.conn.commit()
        self._reopen()

        self.assertEqual(self._row(canonical), (d, e, "resolves", None))
        alias_row = self._row(alias)
        self.assertEqual(alias_row[:3], (d, e, "resolves"))
        self.assertIsNotNone(alias_row[3])

    def test_closed_swap_row_gets_direction_and_predicate_rewritten(self):
        c, d = self._mk_entity("C"), self._mk_entity("D")
        self._reset_to_pre_migration_state()
        now = datetime.now(UTC).isoformat()
        closed = self._mk_relation(c, d, "affects", valid_to=now)  # -> D caused_by C
        self.conn.commit()
        self._reopen()
        row = self._row(closed)
        self.assertEqual(row[0], d)
        self.assertEqual(row[1], c)
        self.assertEqual(row[2], "caused_by")
        self.assertIsNotNone(row[3])

    def test_no_active_row_ends_up_duplicated_after_migration(self):
        a, b, c, d, e = (self._mk_entity(x) for x in "ABCDE")
        self._reset_to_pre_migration_state()
        self._mk_relation(a, b, "relates_to")
        self._mk_relation(a, b, "references")
        self._mk_relation(c, d, "resolved_by")
        self._mk_relation(d, e, "resolves")
        self._mk_relation(e, d, "remediated_by")
        self.conn.commit()
        self._reopen()

        dupes = self.conn.execute(
            "SELECT source_id, target_id, predicate, COUNT(*) c FROM relations "
            "WHERE valid_to IS NULL GROUP BY source_id, target_id, predicate HAVING c > 1"
        ).fetchall()
        self.assertEqual(dupes, [])

    def test_migration_runs_exactly_once_user_version_gate(self):
        a, b = self._mk_entity("A"), self._mk_entity("B")
        self._reset_to_pre_migration_state()
        self._mk_relation(a, b, "relates_to")
        self.conn.commit()
        self._reopen()
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_full_migration_is_idempotent_across_repeated_restarts(self):
        """The single highest-value regression here: re-running init_db() after the migration
        already completed must be a byte-for-byte no-op. This caught a real, pre-existing bug in
        the unrelated dedup-backfill block (schema.py, predates this migration) during
        development -- it grouped duplicate-collapse candidates by (source_id, target_id,
        predicate) without considering valid_to, so a legitimate {active row, closed
        collision-loser row} pair produced by this migration (identical triple, different
        valid_to) looked like a duplicate to it and got silently deleted on the NEXT restart.
        Fixed by scoping that backfill to valid_to IS NULL only, matching the partial UNIQUE
        index it actually exists to protect."""
        a, b, c, d, e = (self._mk_entity(x) for x in "ABCDE")
        self._reset_to_pre_migration_state()
        self._mk_relation(a, b, "relates_to")
        self._mk_relation(a, b, "references")  # collision loser, closed
        self._mk_relation(c, d, "resolved_by")
        self._mk_relation(d, e, "resolves")
        self._mk_relation(e, d, "remediated_by")  # collision loser, closed
        now = datetime.now(UTC).isoformat()
        self._mk_relation(c, d, "affects", valid_to=now)
        self.conn.commit()
        self._reopen()  # runs the migration for real

        before = self.conn.execute(
            "SELECT id, source_id, target_id, predicate, valid_to FROM relations ORDER BY rowid"
        ).fetchall()
        self.assertEqual(len(before), 6, "no row should have been lost by the migration itself")

        self._reopen()  # user_version already current -- must be a pure no-op
        after = self.conn.execute(
            "SELECT id, source_id, target_id, predicate, valid_to FROM relations ORDER BY rowid"
        ).fetchall()
        self.assertEqual(before, after)

        self._reopen()  # and again, for good measure
        after2 = self.conn.execute(
            "SELECT id, source_id, target_id, predicate, valid_to FROM relations ORDER BY rowid"
        ).fetchall()
        self.assertEqual(after, after2)


class TestPredicateRegistryRebuild(_MigrationFixture):
    """§7.2: relates_to/references must repoint from the stale pre-Phase-6 elaborates_on target
    onto related_to, even though they were already migrated (non-NULL canonical_id) before."""

    def test_stale_alias_target_is_corrected(self):
        self._reset_to_pre_migration_state()
        stale = self.conn.execute(
            "SELECT c.name FROM predicates p JOIN predicates c ON c.id = p.canonical_id "
            "WHERE p.name = 'relates_to'"
        ).fetchone()
        self.assertEqual(stale[0], "elaborates_on", "fixture setup sanity check")

        self.conn.commit()
        self._reopen()

        for name in ("relates_to", "references"):
            row = self.conn.execute(
                "SELECT c.name FROM predicates p JOIN predicates c ON c.id = p.canonical_id "
                "WHERE p.name = ?",
                (name,),
            ).fetchone()
            self.assertEqual(row[0], "related_to")

    def test_fresh_db_registry_has_15_canonical_and_36_alias_rows(self):
        # No pre-migration-state reset needed -- setUp's init_db() already ran the migration
        # once on a brand-new DB (user_version starts at 0).
        canonical_count = self.conn.execute(
            "SELECT COUNT(*) FROM predicates WHERE canonical_id IS NULL"
        ).fetchone()[0]
        alias_count = self.conn.execute(
            "SELECT COUNT(*) FROM predicates WHERE canonical_id IS NOT NULL"
        ).fetchone()[0]
        self.assertEqual(canonical_count, 15)
        self.assertEqual(alias_count, 36)


class TestScdHistoryRevisesBackfill(_MigrationFixture):
    """§7.3: pre-immutable-identity `<id>_h_<suffix>` history rows get a backfilled `revises`
    edge from their still-existing canonical entity."""

    def test_backfills_revises_edge_for_existing_history_row(self):
        base = self._mk_entity("Current Version")
        self._reset_to_pre_migration_state()
        hist_id = f"{base}_h_{str(uuid.uuid4())[:8]}"
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, "
            "scope, status, title, full_content, valid_from) VALUES "
            "(?, ?, ?, ?, 'tester', 'shared', 'archived', 'Old Version', 'old body', ?)",
            (hist_id, now, now, now, now),
        )
        self.conn.commit()
        self._reopen()

        row = self.conn.execute(
            "SELECT source_id, target_id FROM relations WHERE target_id = ? AND predicate = "
            "'revises'",
            (hist_id,),
        ).fetchone()
        self.assertEqual(row, (base, hist_id))

    def test_does_not_backfill_when_canonical_entity_is_gone(self):
        self._reset_to_pre_migration_state()
        orphaned_hist_id = f"{uuid.uuid4()}_h_{str(uuid.uuid4())[:8]}"
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, "
            "scope, status, title, full_content, valid_from) VALUES "
            "(?, ?, ?, ?, 'tester', 'shared', 'archived', 'Orphan History', 'old body', ?)",
            (orphaned_hist_id, now, now, now, now),
        )
        self.conn.commit()
        self._reopen()

        row = self.conn.execute(
            "SELECT 1 FROM relations WHERE target_id = ? AND predicate = 'revises'",
            (orphaned_hist_id,),
        ).fetchone()
        self.assertIsNone(row)

    def test_backfill_is_idempotent_no_duplicate_edge_on_second_run(self):
        base = self._mk_entity("Current Version")
        self._reset_to_pre_migration_state()
        hist_id = f"{base}_h_{str(uuid.uuid4())[:8]}"
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, "
            "scope, status, title, full_content, valid_from) VALUES "
            "(?, ?, ?, ?, 'tester', 'shared', 'archived', 'Old Version', 'old body', ?)",
            (hist_id, now, now, now, now),
        )
        self.conn.commit()
        self._reopen()
        self._reopen()  # user_version already current, must not create a second edge

        rows = self.conn.execute(
            "SELECT id FROM relations WHERE target_id = ? AND predicate = 'revises'", (hist_id,)
        ).fetchall()
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
