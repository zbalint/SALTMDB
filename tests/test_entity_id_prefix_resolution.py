import os
import shutil
import tempfile
import unittest

from saltmdb.daemon.db_write_coordinator import DbWriteCoordinator
from saltmdb.daemon.dispatch import dispatch_tool
from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service
from saltmdb.utils.text import resolve_id_prefix


def _store(conn, title, content=None, **kw):
    """Store a memory and return its real UUID (never hardcode IDs, per repo convention).

    content defaults to a title-derived string so distinct calls get distinct content_hash
    values -- store_memory resolves to an existing row by content_hash regardless of
    skip_duplicate_check, so identical default content across calls would silently upsert
    onto the same entity instead of creating separate ones.
    """
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


class TestEntityIdPrefixResolution(unittest.TestCase):
    """fetch_memory_chunk / touch_memory_access short hex-prefix resolution."""

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

    def test_exact_full_uuid_unchanged(self):
        """A full, exact UUID still resolves exactly as before -- regression baseline."""
        entity_id = _store(self.conn, "Exact UUID Fetch Baseline")
        content = memory_service.fetch_memory_chunk(entity_id=entity_id, db_connection=self.conn)
        self.assertIn("Exact UUID Fetch Baseline", content)

    def test_unique_short_prefix_resolves_and_touches(self):
        """A unique 8-char hex prefix resolves to the right entity and bumps last_accessed_at."""
        entity_id = _store(self.conn, "Unique Prefix Target")
        prefix = entity_id[:8]
        self.conn.execute(
            "UPDATE entities SET last_accessed_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (entity_id,),
        )
        self.conn.commit()

        content = memory_service.fetch_memory_chunk(entity_id=prefix, db_connection=self.conn)
        self.assertIn("Unique Prefix Target", content)

        row = self.conn.execute(
            "SELECT last_accessed_at FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertNotEqual(row[0], "2000-01-01T00:00:00+00:00")

    def test_short_prefix_below_minimum_length_is_not_found(self):
        """A prefix shorter than 8 hex chars is treated as a literal (unmatched) id, unchanged."""
        entity_id = _store(self.conn, "Too Short Prefix Target")
        too_short = entity_id[:7]
        content = memory_service.fetch_memory_chunk(entity_id=too_short, db_connection=self.conn)
        self.assertEqual(content, f"Memory not found for ID: {too_short}")

    def test_nonexistent_full_uuid_never_triggers_prefix_scan(self):
        """A full-length UUID that doesn't exist stays a clean 'not found' -- no prefix fallback."""
        bogus_full_uuid = "00000000-0000-4000-8000-000000000000"
        content = memory_service.fetch_memory_chunk(
            entity_id=bogus_full_uuid, db_connection=self.conn
        )
        self.assertEqual(content, f"Memory not found for ID: {bogus_full_uuid}")

    def test_resolve_id_prefix_guard_rejects_full_length_uuid_without_querying(self):
        """White-box check: resolve_id_prefix's >=32-hex-digit guard short-circuits before any
        SQL runs, rather than merely happening to find zero rows via a full table scan -- the
        black-box 'not found' outcome above can't tell those two cases apart, so this asserts
        the no-query claim directly by wrapping the connection and counting execute() calls."""
        full_uuid = "00000000-0000-4000-8000-000000000000"

        class _CountingConn:
            def __init__(self, inner):
                self._inner = inner
                self.execute_calls = 0

            def execute(self, *args, **kw):
                self.execute_calls += 1
                return self._inner.execute(*args, **kw)

        wrapped = _CountingConn(self.conn)
        resolved_id, candidates, truncated = resolve_id_prefix(wrapped, full_uuid)
        self.assertIsNone(resolved_id)
        self.assertEqual(candidates, [])
        self.assertFalse(truncated)
        self.assertEqual(wrapped.execute_calls, 0, "guard must reject before issuing any SQL")

        # Same guard also rejects a 32-char dashless full hex string.
        wrapped = _CountingConn(self.conn)
        resolve_id_prefix(wrapped, full_uuid.replace("-", ""))
        self.assertEqual(wrapped.execute_calls, 0, "guard must reject before issuing any SQL")

    def test_mixed_case_prefix_resolves_same_as_lowercase(self):
        """Prefix matching is case-insensitive."""
        entity_id = _store(self.conn, "Mixed Case Prefix Target")
        prefix_upper = entity_id[:8].upper()
        content = memory_service.fetch_memory_chunk(entity_id=prefix_upper, db_connection=self.conn)
        self.assertIn("Mixed Case Prefix Target", content)

    def test_colliding_prefix_returns_ambiguous_listing_never_content(self):
        """Two entities sharing an 8-char prefix produce a safe candidate listing, not content."""
        id_a = _store(
            self.conn, "Collision Entity A", content="Secret content A that must not leak."
        )
        shared_prefix = id_a[:8]
        # Force a second entity's id to share the same 8-char prefix.
        id_b = _store(
            self.conn, "Collision Entity B", content="Secret content B that must not leak."
        )
        forced_id_b = shared_prefix + id_b[8:]
        self.conn.execute("UPDATE entities SET id = ? WHERE id = ?", (forced_id_b, id_b))
        self.conn.commit()

        content = memory_service.fetch_memory_chunk(
            entity_id=shared_prefix, db_connection=self.conn
        )
        self.assertTrue(content.startswith("Error: Ambiguous ID prefix"))
        self.assertIn(id_a, content)
        self.assertIn(forced_id_b, content)
        self.assertNotIn("Secret content A", content)
        self.assertNotIn("Secret content B", content)

    def test_archived_entity_reachable_via_unique_prefix(self):
        """Archived entities remain reachable by prefix, matching exact-ID fetch's own behavior."""
        entity_id = _store(self.conn, "Archived Prefix Target")
        memory_service.archive_memory(entity_id=entity_id, db_connection=self.conn)
        prefix = entity_id[:8]
        content = memory_service.fetch_memory_chunk(entity_id=prefix, db_connection=self.conn)
        self.assertIn("Archived Prefix Target", content)

    def test_title_like_input_never_triggers_prefix_path(self):
        """A non-hex keyword/title-shaped input never engages prefix resolution."""
        content = memory_service.fetch_memory_chunk(
            entity_id="some totally unrelated title text", db_connection=self.conn
        )
        self.assertEqual(content, "Memory not found for ID: some totally unrelated title text")

    def test_touch_memory_access_with_raw_prefix_updates_correct_row(self):
        """touch_memory_access, given a raw unresolved prefix, still bumps the right row."""
        entity_id = _store(self.conn, "Touch Via Prefix Target")
        prefix = entity_id[:8]
        self.conn.execute(
            "UPDATE entities SET last_accessed_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (entity_id,),
        )
        self.conn.commit()

        memory_service.touch_memory_access(prefix, self.conn)

        row = self.conn.execute(
            "SELECT last_accessed_at FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertNotEqual(row[0], "2000-01-01T00:00:00+00:00")

    def test_touch_memory_access_with_ambiguous_prefix_is_silent_noop(self):
        """touch_memory_access never raises and never guesses on an ambiguous prefix."""
        id_a = _store(self.conn, "Touch Collision A")
        shared_prefix = id_a[:8]
        id_b = _store(self.conn, "Touch Collision B")
        forced_id_b = shared_prefix + id_b[8:]
        self.conn.execute("UPDATE entities SET id = ? WHERE id = ?", (forced_id_b, id_b))
        self.conn.execute(
            "UPDATE entities SET last_accessed_at = '2000-01-01T00:00:00+00:00' WHERE id IN (?, ?)",
            (id_a, forced_id_b),
        )
        self.conn.commit()

        memory_service.touch_memory_access(shared_prefix, self.conn)  # must not raise

        rows = self.conn.execute(
            "SELECT last_accessed_at FROM entities WHERE id IN (?, ?)", (id_a, forced_id_b)
        ).fetchall()
        for (last_accessed_at,) in rows:
            self.assertEqual(last_accessed_at, "2000-01-01T00:00:00+00:00")

    def test_more_than_twenty_collisions_truncate_with_honest_count(self):
        """21+ colliding prefixes report '20+' and 20 candidates, never a fabricated total."""
        shared_prefix = "deadbeef"
        made_ids = []
        for i in range(22):
            entity_id = _store(self.conn, f"Mass Collision Target {i}")
            forced_id = f"{shared_prefix}{entity_id[8:]}"
            self.conn.execute("UPDATE entities SET id = ? WHERE id = ?", (forced_id, entity_id))
            made_ids.append(forced_id)
        self.conn.commit()

        content = memory_service.fetch_memory_chunk(
            entity_id=shared_prefix, db_connection=self.conn
        )
        self.assertIn("20+", content)
        candidate_lines = [
            ln for ln in content.splitlines() if ln.strip().startswith(shared_prefix)
        ]
        self.assertEqual(len(candidate_lines), 20)


class TestEntityIdPrefixResolutionViaDispatch(unittest.TestCase):
    """End-to-end coverage through the real production call site: daemon.dispatch.dispatch_tool."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        conn = init_db(self.db_path)
        conn.close()
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        self.coordinator = DbWriteCoordinator(self.db_path)
        self.coordinator.start()

    def tearDown(self):
        self.coordinator.shutdown(2)
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_dispatch_tool_resolves_prefix_and_touches_last_accessed(self):
        """get_memory(entity_id=<prefix>) returns structured content and touches it."""
        entity_id = self.coordinator.submit(
            "store", lambda conn: _store(conn, "Dispatch Prefix Target")
        )
        self.coordinator.submit(
            "backdate",
            lambda conn: conn.execute(
                "UPDATE entities SET last_accessed_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (entity_id,),
            ),
        )
        prefix = entity_id[:8]

        result = dispatch_tool("get_memory", {"entity_id": prefix}, self.coordinator)
        self.assertEqual(result["status"], "ok")
        self.assertIn("Dispatch Prefix Target", result["data"]["content"])

        last_accessed_at = self.coordinator.submit(
            "check",
            lambda conn: conn.execute(
                "SELECT last_accessed_at FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()[0],
        )
        self.assertNotEqual(last_accessed_at, "2000-01-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
