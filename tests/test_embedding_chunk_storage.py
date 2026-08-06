import unittest
import tempfile
import os
import re
import time

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory, archive_memory
from saltmdb.domain.services import embedding_service
from saltmdb.domain.services.embedding_service import (
    write_entity_chunk_embeddings,
    backfill_chunk_embeddings,
    compute_entity_chunk_embeddings,
)


def _extract_id(store_memory_result: str) -> str:
    match = re.search(r"ID:\s*([a-f0-9\-]+)", store_memory_result)
    assert match, f"Could not parse entity ID from store_memory result: {store_memory_result}"
    return match.group(1)


class TestEmbeddingChunkStorage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def _store(self, title: str, content: str) -> str:
        res = store_memory(
            title=title,
            content=content,
            owner_id="test_user",
            skip_duplicate_check=True,
            db_path=self.db_path,
        )
        return _extract_id(res)

    def _chunk_rows(self, entity_id: str):
        return self.conn.execute(
            "SELECT chunk_index, char_start, char_end FROM entity_chunk_embeddings "
            "WHERE entity_id = ? ORDER BY chunk_index",
            (entity_id,),
        ).fetchall()

    def _content_hash(self, entity_id: str) -> str:
        row = self.conn.execute(
            "SELECT content_hash FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return row[0]

    def _wait_for_entity_embed_settled(self, entity_id: str, timeout_s: float = 5.0) -> str:
        """Polls until store_memory's background _embed_pool has settled this entity's
        (unrelated, entity-level) embedding_status to something other than 'pending' -- avoids
        a race in tests that assert write_entity_chunk_embeddings never touches this column: if
        we read it before the background thread settles, a later background write landing
        mid-test would look (wrongly) like our function under test changed it."""
        deadline = time.monotonic() + timeout_s
        status = None
        while time.monotonic() < deadline:
            status = self.conn.execute(
                "SELECT embedding_status FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()[0]
            if status != "pending":
                return status
            time.sleep(0.05)
        return status

    # -- basic write behavior ------------------------------------------------

    def test_write_creates_correct_rows_and_leaves_embedding_status_untouched(self):
        content = "Content for chunk storage write test, no staleness guard involved here."
        entity_id = self._store("Chunk Storage Write Test", content)

        # Let store_memory's unrelated background entity-level embed settle first, so a
        # concurrent 'pending' -> 'ready' transition from that pool doesn't get misread as
        # write_entity_chunk_embeddings having touched this column itself.
        status_before = self._wait_for_entity_embed_settled(entity_id)

        count = write_entity_chunk_embeddings(entity_id, content, self.db_path)

        status_after = self.conn.execute(
            "SELECT embedding_status FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()[0]

        expected_rows = compute_entity_chunk_embeddings(entity_id, content)
        self.assertEqual(count, len(expected_rows))
        self.assertEqual(len(self._chunk_rows(entity_id)), len(expected_rows))
        self.assertEqual(
            status_before,
            status_after,
            "write_entity_chunk_embeddings must never touch entities.embedding_status",
        )

    def test_reembedding_replaces_old_chunks_with_no_stale_rows(self):
        entity_id = self._store("Reembed Test", "Original short content, one chunk.")
        write_entity_chunk_embeddings(entity_id, "Original short content, one chunk.", self.db_path)
        first_rows = self._chunk_rows(entity_id)
        self.assertEqual(len(first_rows), 1)

        new_content = "".join(f"segment-{i:04d} " for i in range(200))  # forces multiple chunks
        count = write_entity_chunk_embeddings(entity_id, new_content, self.db_path)
        second_rows = self._chunk_rows(entity_id)

        self.assertGreater(len(second_rows), 1)
        self.assertEqual(count, len(second_rows))
        # The old single-chunk row for the original short content must not survive alongside
        # the new rows -- confirms DELETE+INSERT replaced rather than appended.
        self.assertEqual(second_rows[0][1], 0)  # first chunk always starts at char_start=0
        self.assertNotEqual(first_rows[0][2], None)

    # -- staleness guard -------------------------------------------------------

    def test_staleness_guard_skips_on_content_hash_mismatch(self):
        content = "Content whose hash will deliberately not match at write time."
        entity_id = self._store("Staleness Guard Hash Mismatch", content)
        # Phase 2 Part A1 now queues a live async chunk-write trigger on every store_memory
        # call, using the real (correct) content_hash -- wait for it to land, then delete its
        # rows so this test's "starts with zero chunk rows" precondition is deterministic and
        # independent of that unrelated live-trigger race (see the analogous fix in
        # test_backfill_selection_skips_archived_and_already_chunked_entities).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not self._chunk_rows(entity_id):
            time.sleep(0.05)
        self.conn.execute("DELETE FROM entity_chunk_embeddings WHERE entity_id = ?", (entity_id,))

        count = write_entity_chunk_embeddings(
            entity_id, content, self.db_path, expected_content_hash="deliberately-wrong-hash"
        )

        self.assertEqual(count, 0)
        self.assertEqual(self._chunk_rows(entity_id), [])

    def test_staleness_guard_skips_on_archived_status(self):
        content = "Content for an entity that gets archived before the chunk write lands."
        entity_id = self._store("Staleness Guard Archived", content)
        correct_hash = self._content_hash(entity_id)

        archive_memory(entity_id=entity_id, owner_id="test_user", db_path=self.db_path)

        count = write_entity_chunk_embeddings(
            entity_id, content, self.db_path, expected_content_hash=correct_hash
        )

        self.assertEqual(count, 0)
        self.assertEqual(self._chunk_rows(entity_id), [])

    # -- content_hash migration (schema.py) ---------------------------------

    def test_init_db_backfills_null_content_hash_for_legacy_entities(self):
        """schema.py's content_hash migration (Codex re-review fix) must populate NULL
        content_hash for any pre-existing entity when init_db() runs again -- this is the
        root-cause fix, independent of backfill_chunk_embeddings' own defense-in-depth fallback
        tested elsewhere in this file."""
        from saltmdb.utils.text import compute_content_hash

        content = "Legacy entity whose content_hash column predates the migration."
        entity_id = self._store("Legacy Migration Target", content)
        self.conn.execute("UPDATE entities SET content_hash = NULL WHERE id = ?", (entity_id,))
        self.assertIsNone(self._content_hash(entity_id))

        init_db(self.db_path)  # re-run migrations against the same DB file

        self.assertEqual(self._content_hash(entity_id), compute_content_hash(content))

    # -- backfill ----------------------------------------------------------

    def test_backfill_selection_skips_archived_and_already_chunked_entities(self):
        fresh_content = "Fresh entity content, should be picked up by backfill."
        fresh_id = self._store("Backfill Fresh", fresh_content)
        # Phase 2 Part A1 now queues a live async chunk-write trigger on every store_memory
        # call, so a freshly stored entity no longer reliably stays "never chunked" long enough
        # to race against backfill's NOT EXISTS branch below (it settles almost immediately with
        # a warm embedding model). Wait for that live trigger to land, then delete its rows to
        # deterministically reproduce the "genuinely no chunk rows yet" precondition this test
        # exists to check -- equivalent to a legacy/never-triggered entity, independent of
        # whichever pool worker happens to win the live-trigger race.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not self._chunk_rows(fresh_id):
            time.sleep(0.05)
        self.conn.execute("DELETE FROM entity_chunk_embeddings WHERE entity_id = ?", (fresh_id,))
        self.assertEqual(self._chunk_rows(fresh_id), [])

        archived_content = "Archived entity content, must be skipped by the status filter."
        archived_id = self._store("Backfill Archived", archived_content)
        archive_memory(entity_id=archived_id, owner_id="test_user", db_path=self.db_path)

        prechunked_content = "Already-chunked entity content, must be skipped by NOT EXISTS."
        prechunked_id = self._store("Backfill Prechunked", prechunked_content)
        write_entity_chunk_embeddings(prechunked_id, prechunked_content, self.db_path)
        self.assertEqual(len(self._chunk_rows(prechunked_id)), 1)

        written = backfill_chunk_embeddings(self.db_path)

        self.assertEqual(written, 1)
        self.assertGreater(len(self._chunk_rows(fresh_id)), 0)
        self.assertEqual(self._chunk_rows(archived_id), [])
        # Prechunked entity's original row must survive untouched (backfill never re-wrote it).
        self.assertEqual(len(self._chunk_rows(prechunked_id)), 1)

    def test_backfill_skips_entity_mutated_between_select_and_write_deterministic_seam(self):
        """Reproduces the concurrent-update race deterministically via a monkeypatched seam
        (Codex re-review note) rather than real thread timing: the patched
        write_entity_chunk_embeddings mutates the target entity's content_hash in the DB right
        before delegating to the real implementation, simulating a concurrent edit landing in
        the window between backfill's SELECT and its write call for that entity -- every run,
        not dependent on scheduling luck.
        """
        mutated_content = "Content that will be mutated mid-backfill for entity under test."
        target_id = self._store("Backfill Concurrent Mutation Target", mutated_content)
        normal_content = "Unrelated entity content, backfill should still write this one fine."
        normal_id = self._store("Backfill Concurrent Mutation Bystander", normal_content)

        original_write = embedding_service.write_entity_chunk_embeddings
        call_state = {"mutated": False}

        def _seam(entity_id, full_content, db_path, expected_content_hash=None):
            if entity_id == target_id and not call_state["mutated"]:
                call_state["mutated"] = True
                # Deterministically simulate a concurrent write landing between backfill's
                # SELECT (which already captured the OLD content_hash) and this call.
                self.conn.execute(
                    "UPDATE entities SET content_hash = ? WHERE id = ?",
                    ("mutated-by-concurrent-writer", entity_id),
                )
            return original_write(
                entity_id, full_content, db_path, expected_content_hash=expected_content_hash
            )

        embedding_service.write_entity_chunk_embeddings = _seam
        try:
            written = backfill_chunk_embeddings(self.db_path)
        finally:
            embedding_service.write_entity_chunk_embeddings = original_write

        self.assertTrue(call_state["mutated"], "seam must have fired for the target entity")
        self.assertEqual(
            self._chunk_rows(target_id),
            [],
            "the mutated entity must be skipped, not written with stale chunks",
        )
        self.assertGreater(
            len(self._chunk_rows(normal_id)),
            0,
            "the unaffected entity must still be written normally",
        )
        self.assertEqual(
            written, 1, "backfill's return count must exclude the guard-skipped entity"
        )

    def test_backfill_never_writes_unguarded_chunks_for_entity_missing_content_hash(self):
        """Simulates a legacy entity whose content_hash is NULL despite schema.py's migration
        having run (e.g. a caller invoking backfill_chunk_embeddings against a DB that predates
        the migration and hasn't been through a current-code init_db() yet). Before the fix,
        backfill_chunk_embeddings forwarded that NULL straight through as expected_content_hash,
        which write_entity_chunk_embeddings treats as 'staleness guard disabled' -- so this
        entity would have received unguarded chunk rows despite no real fingerprint ever having
        been verified. After the fix, such rows are skipped outright rather than ever calling
        write_entity_chunk_embeddings with a guard-disabling None."""
        content = "Legacy entity content whose content_hash is still unset at backfill time."
        legacy_id = self._store("Legacy Null Content Hash", content)
        self.conn.execute("UPDATE entities SET content_hash = NULL WHERE id = ?", (legacy_id,))
        normal_content = "Unrelated entity with a real content_hash, backfill should write this."
        normal_id = self._store("Legacy Null Content Hash Bystander", normal_content)

        written = backfill_chunk_embeddings(self.db_path)

        self.assertEqual(written, 1, "only the entity with a real content_hash should be written")
        self.assertEqual(
            self._chunk_rows(legacy_id),
            [],
            "an entity with no content_hash must never receive unguarded chunk rows -- this is "
            "the exact bug Codex's Foundation re-review flagged",
        )
        self.assertGreater(len(self._chunk_rows(normal_id)), 0)

    def test_backfill_recovers_after_content_hash_migration_runs(self):
        """Follow-on to the skip test above: once schema.py's content_hash migration actually
        runs (init_db()), the previously-skipped legacy entity gets a real content_hash and
        becomes backfillable -- confirming the skip in backfill_chunk_embeddings is a transient
        'not yet' rather than a permanent dead end."""
        content = "Legacy entity content that becomes backfillable once migrated."
        legacy_id = self._store("Legacy Null Content Hash Recovery", content)
        # store_memory's live async chunk-write trigger (Part A1) can land between _store() and
        # the NULLing below -- wait for it, then clear it, so "starts with zero chunk rows" is
        # deterministic rather than racing the background pool (same fix as
        # test_staleness_guard_skips_on_content_hash_mismatch; became newly reproducible in
        # practice once the entity_chunk_embeddings PARTITION KEY removal made the background
        # write meaningfully faster, narrowing the window this was always racing in).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not self._chunk_rows(legacy_id):
            time.sleep(0.05)
        self.conn.execute("DELETE FROM entity_chunk_embeddings WHERE entity_id = ?", (legacy_id,))
        self.conn.execute("UPDATE entities SET content_hash = NULL WHERE id = ?", (legacy_id,))

        written_before = backfill_chunk_embeddings(self.db_path)
        self.assertEqual(written_before, 0)
        self.assertEqual(self._chunk_rows(legacy_id), [])

        init_db(self.db_path)  # runs schema.py's content_hash backfill migration
        written_after = backfill_chunk_embeddings(self.db_path)

        self.assertEqual(written_after, 1)
        self.assertGreater(len(self._chunk_rows(legacy_id)), 0)


if __name__ == "__main__":
    unittest.main()
