import unittest
import tempfile
import os
import re
import time

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory, archive_memory
from saltmdb.domain.services.relation_service import commit_consolidation
from saltmdb.domain.services.embedding_service import (
    write_entity_chunk_embeddings,
    backfill_chunk_embeddings,
    compute_entity_chunk_embeddings,
)


def _extract_id(result: str) -> str:
    match = re.search(r"ID:\s*([a-f0-9\-]+)", result)
    assert match, f"Could not parse entity ID from result: {result}"
    return match.group(1)


class TestChunkEmbeddingFreshness(unittest.TestCase):
    """Phase 2 Part A -- chunk-embedding freshness lifecycle (see plans/ and SALTMDB memory
    `5c09effa`): entity_chunk_embeddings self-heals the same way entity_embeddings already does,
    now that store_memory/commit_consolidation trigger it live (A1/A2) and the startup sweep can
    tell current rows from stale ones (A0/A3)."""

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

    def _store(self, title: str, content: str, entity_id: str = None) -> str:
        res = store_memory(
            title=title,
            content=content,
            owner_id="test_user",
            skip_duplicate_check=True,
            db_path=self.db_path,
            entity_id=entity_id,
        )
        return _extract_id(res)

    def _chunk_rows(self, entity_id: str):
        return self.conn.execute(
            "SELECT chunk_index, char_start, char_end, content_hash FROM entity_chunk_embeddings "
            "WHERE entity_id = ? ORDER BY chunk_index",
            (entity_id,),
        ).fetchall()

    def _content_hash(self, entity_id: str) -> str:
        row = self.conn.execute(
            "SELECT content_hash FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return row[0] if row else None

    def _full_content(self, entity_id: str) -> str:
        row = self.conn.execute(
            "SELECT full_content FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return row[0] if row else None

    def _poll_for_rows(self, entity_id: str, tries: int = 50, interval: float = 0.1):
        """Poll up to tries * interval seconds (default 5s) for entity_chunk_embeddings rows to
        appear -- mirrors the poll-loop pattern of test_embedding_trigger.py /
        test_consolidation_embedding_trigger.py, adapted to check chunk-row presence instead of
        entities.embedding_status."""
        rows = []
        for _ in range(tries):
            rows = self._chunk_rows(entity_id)
            if rows:
                return rows
            time.sleep(interval)
        return rows

    # -- A1: store_memory create/edit trigger ---------------------------------

    def test_store_memory_create_produces_chunk_rows_with_matching_content_hash(self):
        content = "Content for the chunk-embedding freshness trigger test on store_memory create."
        entity_id = self._store("Chunk Freshness Create Trigger", content)

        rows = self._poll_for_rows(entity_id)
        self.assertTrue(rows, "entity_chunk_embeddings rows did not appear for the new entity")

        # Compare against the entity's actually-stored content (post auto-format/redaction),
        # not the raw input string, since that's what the real async trigger chunks.
        stored_content = self._full_content(entity_id)
        expected_rows = compute_entity_chunk_embeddings(entity_id, stored_content)
        self.assertEqual(len(rows), len(expected_rows))

        entity_hash = self._content_hash(entity_id)
        self.assertTrue(entity_hash)
        for _chunk_index, _char_start, _char_end, row_hash in rows:
            self.assertEqual(row_hash, entity_hash)

    def test_store_memory_edit_refreshes_chunk_rows_not_appends(self):
        content_a = "Original content A, short and simple, one chunk expected here."
        entity_id = self._store("Chunk Freshness Edit Trigger", content_a)

        rows_a = self._poll_for_rows(entity_id)
        self.assertTrue(rows_a)
        hash_a = self._content_hash(entity_id)

        # Materially different (much longer) content -> forces a different chunk boundary set.
        # Real, non-repetitive prose (not a repeated token pattern): store_memory's quality gate
        # rejects both unrealistic readability scores and high n-gram repetition, unlike
        # write_entity_chunk_embeddings called directly (see test_embedding_chunk_storage.py),
        # which bypasses that gate entirely.
        content_b = " ".join(
            [
                "The distributed cache invalidation protocol propagates updates asynchronously across all replica nodes in the cluster.",
                "Each node maintains a local version vector to detect stale reads before serving a client request.",
                "When a write lands on the primary shard, it is fanned out to every follower via a low-latency gossip channel.",
                "Followers apply the update only after verifying the incoming version number is strictly newer than their own.",
                "Conflicts are resolved using a last-writer-wins policy keyed on a hybrid logical clock timestamp.",
                "Background compaction periodically merges tombstoned entries to keep the working set memory-efficient.",
                "Operators can tune the gossip fan-out factor to trade convergence latency against network overhead.",
                "A dedicated health-check loop demotes any node whose heartbeat lags beyond the configured timeout window.",
                "Once demoted, the node is excluded from the write quorum until it catches up on the replication log.",
                "This design keeps read latency low while still offering eventual consistency guarantees across regions.",
                "Snapshot isolation is achieved by pinning a read timestamp at the start of every transaction.",
                "A leader election algorithm based on raft consensus prevents split-brain scenarios during a network partition.",
                "Throughput scales roughly linearly as additional shards are added to the cluster topology.",
                "Metrics such as replication lag and queue depth are exported to the monitoring dashboard every ten seconds.",
                "Client libraries automatically retry idempotent requests against a different replica on timeout.",
                "The garbage collector reclaims expired tombstones only after every replica has acknowledged the delete.",
                "Configuration changes are rolled out gradually using a canary deployment strategy across availability zones.",
                "Encryption at rest is enabled by default for the underlying storage volume of every shard.",
                "A circuit breaker trips automatically when downstream error rates exceed a configurable threshold.",
                "Capacity planning relies on historical write amplification data collected over the previous quarter.",
            ]
        )
        self._store("Chunk Freshness Edit Trigger", content_b, entity_id=entity_id)

        # Poll until the edit's own content_hash actually commits.
        hash_b = None
        hash_committed = False
        for _ in range(50):
            hash_b = self._content_hash(entity_id)
            if hash_b and hash_b != hash_a:
                hash_committed = True
                break
            time.sleep(0.1)
        self.assertTrue(hash_committed, "entities.content_hash did not update after the edit")

        # Poll until chunk rows fully reflect the new content_hash -- if any stale
        # content_A-era row survived (DELETE+INSERT didn't replace cleanly), this uniform
        # equality check across every row would fail.
        rows_b = []
        refreshed = False
        for _ in range(50):
            rows_b = self._chunk_rows(entity_id)
            if rows_b and all(r[3] == hash_b for r in rows_b):
                refreshed = True
                break
            time.sleep(0.1)
        self.assertTrue(refreshed, "chunk rows did not refresh to the new content_hash after edit")

        self.assertGreater(
            len(rows_b), len(rows_a), "much longer content_b should produce more chunks"
        )
        boundaries_a = {(r[0], r[1], r[2]) for r in rows_a}
        boundaries_b = {(r[0], r[1], r[2]) for r in rows_b}
        self.assertNotEqual(boundaries_a, boundaries_b)

    # -- A2: commit_consolidation trigger --------------------------------------

    def test_commit_consolidation_produces_chunk_rows_with_matching_content_hash(self):
        id1 = self._store(
            "Consolidation Parent A", "Detailed description of Fact A for chunk freshness test."
        )
        id2 = self._store(
            "Consolidation Parent B", "Detailed description of Fact B for chunk freshness test."
        )

        c_res = commit_consolidation(
            parent_ids=[id1, id2],
            title="Consolidated Chunk Freshness Overview",
            content="Merged summary of A and B for the chunk-embedding freshness trigger test.",
            tags=["#summary"],
            owner_id="test_user",
            db_connection=self.conn,
        )
        self.assertIn("Successfully committed", c_res)
        consolidated_id = c_res.split("ID: ")[-1].strip()

        rows = self._poll_for_rows(consolidated_id)
        self.assertTrue(
            rows, "entity_chunk_embeddings rows did not appear for the consolidated entity"
        )

        entity_hash = self._content_hash(consolidated_id)
        self.assertTrue(entity_hash)
        for row in rows:
            self.assertEqual(row[3], entity_hash)

    # -- A4: archive-time deletion regression guard ----------------------------

    def test_archive_memory_does_not_delete_chunk_rows(self):
        content = "Content for the archive-time-deletion regression guard (Part A4)."
        entity_id = self._store("Chunk Freshness Archive Guard", content)

        rows_before = self._poll_for_rows(entity_id)
        self.assertTrue(rows_before)

        archive_result = archive_memory(
            entity_id=entity_id, owner_id="test_user", db_path=self.db_path
        )
        self.assertIn("archived", archive_result.lower())

        rows_after = self._chunk_rows(entity_id)
        self.assertEqual(
            len(rows_after),
            len(rows_before),
            "archive_memory must never delete entity_chunk_embeddings rows (A4 precedent)",
        )

    # -- Codex-required: deterministic reverse-completion race ----------------

    def test_reverse_completion_race_stale_write_does_not_clobber_newer_commit(self):
        """Simulates two _embed_pool chunk-write jobs for the same entity_id completing out of
        commit order, without real thread-timing flakiness: store A, store B (a real edit over
        the same entity_id, whose own async chunk job is awaited/polled to completion first),
        then directly call write_entity_chunk_embeddings with A's stale
        (entity_id, content_A, expected_content_hash=hash_A) as if A's worker just now got
        scheduled -- after B already committed. Must no-op, not clobber B's current rows."""
        content_a = "Content A for the deterministic reverse-completion race test."
        entity_id = self._store("Reverse Completion Race", content_a)
        rows_a = self._poll_for_rows(entity_id)
        self.assertTrue(rows_a)
        hash_a = self._content_hash(entity_id)

        content_b = "Content B, materially different, committed over A before A's chunk job runs."
        self._store("Reverse Completion Race", content_b, entity_id=entity_id)

        # Wait for B's own real async chunk job to land current chunks first.
        hash_b = None
        settled = False
        for _ in range(50):
            hash_b = self._content_hash(entity_id)
            if hash_b and hash_b != hash_a:
                rows_now = self._chunk_rows(entity_id)
                if rows_now and all(r[3] == hash_b for r in rows_now):
                    settled = True
                    break
            time.sleep(0.1)
        self.assertTrue(settled, "content B's chunk rows did not settle before simulating the race")

        # Simulate A's _embed_pool worker finally running AFTER B's, directly and synchronously
        # (thread-pool scheduling order, not commit order -- the exact race Codex flagged).
        count = write_entity_chunk_embeddings(
            entity_id, content_a, self.db_path, expected_content_hash=hash_a
        )

        self.assertEqual(
            count, 0, "a stale in-flight write for an old content_hash must no-op, not write"
        )
        rows_after = self._chunk_rows(entity_id)
        self.assertTrue(rows_after)
        for row in rows_after:
            self.assertEqual(
                row[3], hash_b, "chunk rows must still reflect content_B/hash_B, not be clobbered"
            )

    # -- Codex-required: stale-but-present repair ------------------------------

    def test_backfill_repairs_stale_but_present_chunk_rows(self):
        """Directly manipulates a stored entity's chunk rows to have a content_hash that no
        longer matches entities.content_hash (simulating a Foundation-era row or a missed
        refresh). Asserts the OLD NOT-EXISTS-only selection would have skipped it (rows are
        present, just stale), then calls backfill_chunk_embeddings (A3's real query) and asserts
        it's now selected, re-embedded, and its chunk rows' content_hash matches
        entities.content_hash afterward."""
        content = (
            "Content whose chunk rows will be deliberately staled to simulate a missed refresh."
        )
        entity_id = self._store("Stale But Present Repair", content)
        rows = self._poll_for_rows(entity_id)
        self.assertTrue(rows)

        # vec0 rejects UPDATE predicated on the PARTITION KEY column (entity_id) outright
        # ("UPDATE on partition key columns are not supported yet"), so update row-by-row via
        # the actual primary key instead.
        row_ids = [
            r[0]
            for r in self.conn.execute(
                "SELECT id FROM entity_chunk_embeddings WHERE entity_id = ?", (entity_id,)
            ).fetchall()
        ]
        self.assertTrue(row_ids)
        for row_id in row_ids:
            self.conn.execute(
                "UPDATE entity_chunk_embeddings SET content_hash = ? WHERE id = ?",
                ("stale-content-hash-does-not-match", row_id),
            )
        stale_rows = self._chunk_rows(entity_id)
        self.assertTrue(stale_rows)
        for row in stale_rows:
            self.assertEqual(row[3], "stale-content-hash-does-not-match")

        # The OLD (pre-A3) NOT EXISTS-only selection predicate would skip this entity entirely,
        # since it still has rows -- just stale ones. This is the exact gap A3 closes.
        old_style_selected = self.conn.execute(
            "SELECT e.id FROM entities e WHERE e.status != 'archived' AND NOT EXISTS "
            "(SELECT 1 FROM entity_chunk_embeddings c WHERE c.entity_id = e.id) AND e.id = ?",
            (entity_id,),
        ).fetchall()
        self.assertEqual(
            old_style_selected,
            [],
            "old NOT EXISTS-only selection incorrectly would have skipped a stale-but-present entity",
        )

        written = backfill_chunk_embeddings(self.db_path)

        self.assertGreaterEqual(written, 1)
        repaired_rows = self._chunk_rows(entity_id)
        self.assertTrue(repaired_rows)
        entity_hash = self._content_hash(entity_id)
        for row in repaired_rows:
            self.assertEqual(row[3], entity_hash)


if __name__ == "__main__":
    unittest.main()
