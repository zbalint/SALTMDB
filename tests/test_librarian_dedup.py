import unittest
import tempfile
import os
import shutil
import json
from datetime import datetime, UTC

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services import librarian_service


class TestLibrarianConsolidationRequestDedup(unittest.TestCase):
    """Regression coverage for the unbounded consolidation_request growth found via
    librarian.log: every librarian run used to re-log an identical event for the same
    still-unprocessed backlog forever, since nothing checked whether a prior request for
    the same target was already pending. Each redundant insert cost its own write
    transaction, and the per-run write count only ever grew as a session went on --
    directly compounding lock contention alongside the trigger_librarian hot-path fix.
    """

    def setUp(self):
        os.environ["SALTMDB_DISABLE_LIBRARIAN"] = "1"
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        os.environ.pop("SALTMDB_DISABLE_LIBRARIAN", None)

    def _store(self, title, tags=None):
        res = store_memory(
            content=f"Body content for {title}, padded to satisfy the quality gate minimum length requirement.",
            title=title,
            owner_id="tester",
            tags=tags,
            skip_duplicate_check=True,
            db_connection=self.conn,
            db_path=self.db_path,
        )
        self.assertFalse(res.startswith("Error"), res)
        # store_memory appends " [Tip: ...]" after the ID when tags are provided, so split
        # on whitespace too rather than assuming "ID: <uuid>" is the whole tail of the string.
        return res.split("ID: ")[1].split()[0]

    def _consolidation_request_count(self, target=None):
        rows = self.conn.execute(
            "SELECT content FROM events WHERE type = 'consolidation_request'"
        ).fetchall()
        if target is None:
            return len(rows)
        return sum(1 for (c,) in rows if json.loads(c).get("target") == target)

    def test_cluttered_tags_pass_does_not_reduplicate_pending_request(self):
        for i in range(5):
            self._store(f"Cluttered Tag Entity {i}", tags=["#dedupclutter"])

        librarian_service.consolidate_cluttered_tags(conn=self.conn)
        self.assertEqual(self._consolidation_request_count("tag"), 1)

        # Running the same pass again with nothing resolved must NOT log a second event
        # for the same tag -- this is the exact regression: unbounded re-logging on every run.
        librarian_service.consolidate_cluttered_tags(conn=self.conn)
        librarian_service.consolidate_cluttered_tags(conn=self.conn)
        self.assertEqual(self._consolidation_request_count("tag"), 1)

    def test_cluttered_tags_pass_logs_again_once_prior_request_resolved(self):
        ids = [self._store(f"Cluttered Tag Entity B{i}", tags=["#dedupclutter2"]) for i in range(5)]
        librarian_service.consolidate_cluttered_tags(conn=self.conn)
        self.assertEqual(self._consolidation_request_count("tag"), 1)

        # Resolve the backlog the first request was waiting on.
        datetime.now(UTC).isoformat()
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(
            f"UPDATE entities SET status = 'archived' WHERE id IN ({placeholders})", ids
        )
        self.conn.commit()

        for i in range(5):
            self._store(f"Cluttered Tag Entity C{i}", tags=["#dedupclutter2"])

        librarian_service.consolidate_cluttered_tags(conn=self.conn)
        self.assertEqual(self._consolidation_request_count("tag"), 2)

    def test_supersession_scouting_does_not_reduplicate_pending_request(self):
        # scout_consolidated_supersessions requires sqlite-vec + embeddings; skip gracefully
        # in environments where the extension truly can't load (mirrors the function's own
        # try/except Exception: return contract).
        try:
            import sqlite_vec  # noqa: F401
        except Exception:
            self.skipTest("sqlite-vec not available in this environment")

        librarian_service.scout_consolidated_supersessions(conn=self.conn)
        librarian_service.scout_consolidated_supersessions(conn=self.conn)
        # With no consolidated/embedded entities at all, both runs are no-ops -- this just
        # proves the pass tolerates being called repeatedly without erroring or growing state.
        self.assertEqual(self._consolidation_request_count("supersession_candidate"), 0)

    def test_pending_request_exists_helper_true_only_while_unresolved(self):
        ids = [self._store(f"Helper Probe Entity {i}", tags=["#helperprobe"]) for i in range(5)]
        librarian_service.consolidate_cluttered_tags(conn=self.conn)

        self.assertTrue(
            librarian_service._pending_request_exists(self.conn, "tag", tag_name="#helperprobe")
        )

        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(
            f"UPDATE entities SET status = 'archived' WHERE id IN ({placeholders})", ids
        )
        self.conn.commit()

        self.assertFalse(
            librarian_service._pending_request_exists(self.conn, "tag", tag_name="#helperprobe")
        )


if __name__ == "__main__":
    unittest.main()
