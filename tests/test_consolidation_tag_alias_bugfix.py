import unittest
import tempfile
import os
import shutil
from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service, librarian_service, relation_service


class TestConsolidationTagAliasBugfix(unittest.TestCase):
    """Regression test for the commit_consolidation() tag-resolution bugfix.

    Before the fix, commit_consolidation() resolved tags via an inline exact-name-match
    query (`SELECT id FROM tags WHERE name = ?`) that completely ignored `canonical_id`
    aliasing and never populated `normalized_name` on newly created rows. This meant a
    consolidated entity tagged with an already-merged/aliased tag name would end up
    pointing at the stale alias tag id instead of following the alias to its canonical
    tag -- silently re-fragmenting the folksonomy that merge_tags() had just cleaned up.

    This test proves the fix: commit_consolidation() now shares resolve_or_create_tag()
    with store_memory(), so it correctly follows existing aliases and correctly populates
    normalized_name for brand-new tags.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _tag_row(self, name):
        return self.conn.execute(
            "SELECT id, canonical_id, normalized_name FROM tags WHERE lower(name) = lower(?)",
            (name,),
        ).fetchone()

    def test_commit_consolidation_follows_existing_alias_to_canonical_tag(self):
        # 1. Store an entity tagged #bugfix.
        res1 = memory_service.store_memory(
            content="Root cause of the nightly job crash was an unhandled null pointer in the retry handler.",
            title="Nightly Job Crash Root Cause Bugfix",
            tags=["#bugfix"],
            owner_id="user1",
            db_connection=self.conn,
        )
        self.assertEqual(res1["status"], "ok")
        id1 = res1["data"]["id"]

        # 2. Store a second, genuinely different-looking raw entity tagged #fix (not a
        #    plural/suffix variant of #bugfix, so it lands as its own row -- this exercises
        #    the alias path, not the plural-fallback path).
        res2 = memory_service.store_memory(
            content="Applied a targeted patch to the retry handler to null-check the response before dereferencing.",
            title="Retry Handler Null Check Fix",
            tags=["#fix"],
            owner_id="user1",
            db_connection=self.conn,
        )
        self.assertEqual(res2["status"], "ok")
        id2 = res2["data"]["id"]

        bugfix_row = self._tag_row("#bugfix")
        fix_row = self._tag_row("#fix")
        self.assertIsNotNone(bugfix_row)
        self.assertIsNotNone(fix_row)
        bugfix_tag_id = bugfix_row[0]
        fix_tag_id = fix_row[0]
        self.assertNotEqual(
            bugfix_tag_id, fix_tag_id, "#bugfix and #fix must start as two distinct rows"
        )

        # 3. Merge #fix into #bugfix (simulating a prior Librarian merge_tags call).
        merge_res = librarian_service.merge_tags(
            keep_tag="#bugfix", tags_to_merge=["#fix"], conn=self.conn
        )
        self.assertIn("Merged 1 tag(s)", merge_res)

        fix_row_after_merge = self._tag_row("#fix")
        self.assertEqual(
            fix_row_after_merge[1],
            bugfix_tag_id,
            "#fix's canonical_id should now point at #bugfix's tag id",
        )

        # 4. Commit a consolidation over both raw parents, tagging the new consolidated
        #    entity with the now-aliased "#fix" name, plus one brand-new tag never seen before.
        consolidation_res = relation_service.commit_consolidation(
            parent_ids=[id1, id2],
            title="Nightly Job Crash: Root Cause and Fix Consolidated",
            content=(
                "Consolidated record: the nightly job crash was caused by an unhandled null "
                "pointer in the retry handler, and was resolved by adding a null check before "
                "dereferencing the response object."
            ),
            tags=["#fix", "#anewconsolidationtag"],
            owner_id="user1",
            db_connection=self.conn,
            # Real cosine similarity between these two parents sits at ~0.73, above the 0.60
            # placeholder threshold but with a margin that shouldn't be relied on once the real
            # benchmark locks the final value -- this test is about tag-alias resolution, not
            # the cohesion gate.
            override_justification="pre-existing test fixture, not exercising the cohesion gate",
        )
        self.assertIn("Successfully committed consolidated memory", consolidation_res)
        consolidated_id = consolidation_res.split("ID: ")[1].strip()

        # 5. Assert the consolidated entity's entity_tags row for the "#fix" tag points at
        #    #bugfix's CANONICAL tag id, not #fix's aliased tag id.
        entity_tag_ids = {
            r[0]
            for r in self.conn.execute(
                "SELECT tag_id FROM entity_tags WHERE entity_id = ?", (consolidated_id,)
            ).fetchall()
        }
        self.assertIn(
            bugfix_tag_id,
            entity_tag_ids,
            "consolidated entity must be tagged with #bugfix's canonical tag id",
        )
        self.assertNotIn(
            fix_tag_id,
            entity_tag_ids,
            "consolidated entity must NOT be tagged with #fix's stale aliased tag id",
        )

        # 6. Assert the brand-new tag's row got normalized_name populated -- the other half
        #    of the bug (commit_consolidation's old inline path never set normalized_name on
        #    newly created tag rows).
        new_tag_row = self._tag_row("#anewconsolidationtag")
        self.assertIsNotNone(new_tag_row, "the new tag should have been created")
        self.assertIsNotNone(
            new_tag_row[2],
            "commit_consolidation must populate normalized_name for newly created tags",
        )
        self.assertEqual(new_tag_row[2], "anewconsolidationtag")


if __name__ == "__main__":
    unittest.main()
