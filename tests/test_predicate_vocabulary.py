import tempfile
import os
import shutil
import unittest

from saltmdb.utils.predicate_vocabulary import (
    AGENT_SELECTABLE_PREDICATES,
    RESERVED_PREDICATES,
    LEGACY_READONLY_PREDICATES,
    PREDICATE_ALIASES,
    classify_predicate,
    normalize_predicate_name,
)
from saltmdb.db.schema import init_db


class TestClassifyPredicate(unittest.TestCase):
    """Pure-Python coverage of the closed relation-predicate vocabulary (agent API redesign
    plan §5.8, Phase 6 item 25) -- zero DB access, mirrors test_envelope.py/test_corrected_call.py's
    style for a shared-module-only test file."""

    def test_selectable_status(self):
        disposition = classify_predicate("depends_on")
        self.assertEqual(disposition.status, "selectable")
        self.assertEqual(disposition.canonical, "depends_on")
        self.assertFalse(disposition.swap)
        self.assertIsNone(disposition.lifecycle_tool)

    def test_alias_same_direction_status(self):
        disposition = classify_predicate("relates_to")
        self.assertEqual(disposition.status, "alias")
        self.assertEqual(disposition.canonical, "related_to")
        self.assertFalse(disposition.swap)
        self.assertIsNone(disposition.lifecycle_tool)

    def test_alias_with_swap_status(self):
        disposition = classify_predicate("resolved_by")
        self.assertEqual(disposition.status, "alias")
        self.assertEqual(disposition.canonical, "resolves")
        self.assertTrue(disposition.swap)
        self.assertIsNone(disposition.lifecycle_tool)

    def test_reserved_status_supersedes(self):
        disposition = classify_predicate("supersedes")
        self.assertEqual(disposition.status, "reserved")
        self.assertEqual(disposition.canonical, "supersedes")
        self.assertFalse(disposition.swap)
        self.assertEqual(disposition.lifecycle_tool, "supersede_memory")

    def test_reserved_status_consolidated_from(self):
        disposition = classify_predicate("consolidated_from")
        self.assertEqual(disposition.status, "reserved")
        self.assertEqual(disposition.lifecycle_tool, "consolidate_memories")

    def test_reserved_status_revises(self):
        disposition = classify_predicate("revises")
        self.assertEqual(disposition.status, "reserved")
        self.assertEqual(disposition.lifecycle_tool, "revise_memory")

    def test_legacy_readonly_status(self):
        disposition = classify_predicate("similar_to")
        self.assertEqual(disposition.status, "legacy_readonly")
        self.assertEqual(disposition.canonical, "similar_to")
        self.assertFalse(disposition.swap)
        self.assertIsNone(disposition.lifecycle_tool)

    def test_unknown_status(self):
        disposition = classify_predicate("totally_made_up_predicate")
        self.assertEqual(disposition.status, "unknown")
        self.assertIsNone(disposition.canonical)
        self.assertFalse(disposition.swap)
        self.assertIsNone(disposition.lifecycle_tool)

    def test_unknown_status_for_none_and_empty_and_degenerate_input(self):
        for raw in (None, "", "   ", "!!!"):
            disposition = classify_predicate(raw)
            self.assertEqual(disposition.status, "unknown", f"raw={raw!r}")
            self.assertIsNone(disposition.canonical)

    def test_normalization_is_case_and_format_insensitive(self):
        self.assertEqual(classify_predicate("Depends-On").status, "selectable")
        self.assertEqual(classify_predicate("Depends-On").canonical, "depends_on")
        self.assertEqual(classify_predicate("depends on").canonical, "depends_on")
        self.assertEqual(normalize_predicate_name("Depends-On"), "depends_on")
        self.assertEqual(normalize_predicate_name("  depends   on  "), "depends_on")

    def test_closed_universe_is_exactly_51_names_with_no_overlaps(self):
        selectable = set(AGENT_SELECTABLE_PREDICATES)
        reserved = set(RESERVED_PREDICATES)
        legacy = set(LEGACY_READONLY_PREDICATES)
        aliases = set(PREDICATE_ALIASES)

        self.assertEqual(len(selectable), 11)
        self.assertEqual(len(reserved), 3)
        self.assertEqual(len(legacy), 1)
        self.assertEqual(len(aliases), 36)

        all_names = selectable | reserved | legacy | aliases
        self.assertEqual(
            len(all_names),
            51,
            "the four categories must be pairwise disjoint and total exactly 51 names",
        )
        # Pairwise disjointness, spelled out explicitly rather than inferred only from the
        # summed cardinality above (a coincidental overlap could still add up to 51).
        self.assertEqual(selectable & reserved, set())
        self.assertEqual(selectable & legacy, set())
        self.assertEqual(selectable & aliases, set())
        self.assertEqual(reserved & legacy, set())
        self.assertEqual(reserved & aliases, set())
        self.assertEqual(legacy & aliases, set())

    def test_every_alias_canonical_target_is_agent_selectable(self):
        # Aliases only ever point at an agent-selectable predicate -- never at a reserved or
        # legacy-readonly one (nothing "drifts into" a system-owned or frozen predicate).
        for alias_name, (canonical, _swap) in PREDICATE_ALIASES.items():
            self.assertIn(
                canonical,
                AGENT_SELECTABLE_PREDICATES,
                f"alias '{alias_name}' canonicalizes to '{canonical}', which is not "
                "agent-selectable",
            )

    def test_reserved_predicates_map_to_their_lifecycle_tool_names(self):
        self.assertEqual(
            RESERVED_PREDICATES,
            {
                "supersedes": "supersede_memory",
                "consolidated_from": "consolidate_memories",
                "revises": "revise_memory",
            },
        )


class TestInitDbPredicateSeeding(unittest.TestCase):
    """Fresh init_db() regression (Phase 6 item 25/§7.1): the predicates table seed must mirror
    saltmdb.utils.predicate_vocabulary exactly, including the reversed relates_to/references ->
    related_to alias target (previously elaborates_on)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_fresh_db_has_exactly_51_predicate_rows(self):
        count = self.conn.execute("SELECT COUNT(*) FROM predicates").fetchone()[0]
        self.assertEqual(count, 51)

    def test_fresh_db_has_exactly_15_canonical_rows(self):
        count = self.conn.execute(
            "SELECT COUNT(*) FROM predicates WHERE canonical_id IS NULL"
        ).fetchone()[0]
        self.assertEqual(count, 15)

    def test_relates_to_and_references_alias_onto_related_to_not_elaborates_on(self):
        # The reversed-behavior regression that matters most here (plan §3.17/§5.8): pre-Phase-6,
        # both aliased onto elaborates_on.
        for alias_name in ("relates_to", "references"):
            row = self.conn.execute(
                "SELECT c.name FROM predicates p JOIN predicates c ON c.id = p.canonical_id "
                "WHERE p.name = ?",
                (alias_name,),
            ).fetchone()
            self.assertIsNotNone(row, f"'{alias_name}' must exist as a seeded predicate row")
            self.assertEqual(
                row[0],
                "related_to",
                f"'{alias_name}' must alias onto 'related_to', not 'elaborates_on'",
            )

    def test_every_predicate_vocabulary_name_is_seeded(self):
        rows = self.conn.execute("SELECT name FROM predicates").fetchall()
        seeded_names = {r[0] for r in rows}
        expected_names = (
            AGENT_SELECTABLE_PREDICATES
            | set(RESERVED_PREDICATES)
            | LEGACY_READONLY_PREDICATES
            | set(PREDICATE_ALIASES)
        )
        self.assertTrue(expected_names.issubset(seeded_names))


if __name__ == "__main__":
    unittest.main()
