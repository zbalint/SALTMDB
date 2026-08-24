import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import get_memory, store_memory


class TestGetMemory(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "get-memory.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert(self, entity_id, title, status="raw"):
        self.conn.execute(
            """INSERT INTO entities
               (id, created_at, updated_at, last_accessed_at, title, full_content, status)
               VALUES (?, datetime('now'), datetime('now'), datetime('now'), ?, ?, ?)""",
            (entity_id, title, f"# {title}\n\nFull body for {title}.", status),
        )
        self.conn.commit()

    def test_returns_full_active_memory_in_envelope(self):
        entity_id = "12345678-1234-1234-1234-123456789abc"
        self._insert(entity_id, "Active memory")

        result = get_memory(entity_id="12345678", db_connection=self.conn)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["id"], entity_id)
        self.assertEqual(result["data"]["status"], "raw")
        self.assertIn("Full body", result["data"]["content"])
        self.assertEqual(result["data"]["lineage"], {"ancestors": [], "descendants": []})

    def _attach_tag(self, entity_id, tag_name):
        tag_id = f"tag-{tag_name.lstrip('#')}"
        self.conn.execute(
            "INSERT OR IGNORE INTO tags (id, name, normalized_name) VALUES (?, ?, ?)",
            (tag_id, tag_name, tag_name.lstrip("#")),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)",
            (entity_id, tag_id),
        )
        self.conn.commit()

    def test_returns_entity_tags_excluding_core_marker(self):
        entity_id = "11112222-1234-1234-1234-123456789abc"
        self._insert(entity_id, "Tagged memory")
        self._attach_tag(entity_id, "#cadet")
        self._attach_tag(entity_id, "#bug")
        self._attach_tag(entity_id, "#core")

        result = get_memory(entity_id=entity_id, db_connection=self.conn)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["tags"], ["#bug", "#cadet"])

    def test_returns_archived_entity_without_redirecting(self):
        archived = "abcdefab-1234-1234-1234-123456789abc"
        successor = "fedcba98-1234-1234-1234-123456789abc"
        self._insert(archived, "Archived memory", status="archived")
        self._insert(successor, "Successor memory")

        with patch(
            "saltmdb.domain.services.relation_service.get_lineage",
            return_value={"nodes": [{"id": successor, "depth": 1, "status": "raw"}]},
            create=True,
        ) as lineage:
            result = get_memory(entity_id=archived, db_connection=self.conn)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["id"], archived)
        self.assertEqual(result["data"]["status"], "archived")
        self.assertEqual(lineage.call_count, 2)

    def test_ambiguous_prefix_is_structured_error(self):
        self._insert("a1b2c3d4-1234-1234-1234-123456789abc", "One")
        self._insert("a1b2c3d4-5678-1234-1234-123456789abc", "Two")

        result = get_memory(entity_id="a1b2c3d4", db_connection=self.conn)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "AMBIGUOUS_ID_PREFIX")
        self.assertEqual({c["title"] for c in result["errors"][0]["candidates"]}, {"One", "Two"})

    def test_unknown_id_is_structured_error(self):
        result = get_memory(entity_id="does-not-exist", db_connection=self.conn)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "UNKNOWN_ENTITY_ID")

    def test_returns_agent_session_id_and_last_touched_session_id(self):
        # Regression test: agent_session_id/last_touched_session_id are written on every
        # store_memory call (verified separately) but were never read back by get_memory --
        # the SELECT simply omitted both columns. A caller could filter search_memory by
        # agent_session_id but never actually see a memory's value. This asserts the value
        # itself is present in the response, not just that the right row can be filtered to.
        entity_id = "22223333-1234-1234-1234-123456789abc"
        self._insert(entity_id, "Session-stamped memory")
        self.conn.execute(
            "UPDATE entities SET agent_session_id = ?, last_touched_session_id = ? WHERE id = ?",
            ("session-created-aaaa", "session-touched-bbbb", entity_id),
        )
        self.conn.commit()

        result = get_memory(entity_id=entity_id, db_connection=self.conn)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["agent_session_id"], "session-created-aaaa")
        self.assertEqual(result["data"]["last_touched_session_id"], "session-touched-bbbb")

    def test_agent_session_id_is_none_when_never_stamped(self):
        # Historical rows predating this feature have NULL in both columns -- get_memory
        # must surface that as None, not omit the keys or raise.
        entity_id = "44445555-1234-1234-1234-123456789abc"
        self._insert(entity_id, "Pre-feature memory")

        result = get_memory(entity_id=entity_id, db_connection=self.conn)

        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["data"]["agent_session_id"])
        self.assertIsNone(result["data"]["last_touched_session_id"])

    def test_store_memory_effective_block_reports_session_id_and_get_memory_round_trips_it(self):
        # Regression test: store_memory's response already reports back the effective
        # owner_id/context_id/scope/memory_type a write actually used -- agent_session_id was
        # stamped on the entities row correctly but never surfaced in that same effective
        # block, so a caller had no way to learn *which* session id to later look for. This
        # confirms both halves: the write response reports it, and get_memory reads the same
        # value back off the row it produced.
        result = store_memory(
            title="[Session] Effective-block probe",
            content="Confirms agent_session_id round-trips through store_memory's effective block.",
            tags=["#session-id"],
            owner_id="agent_qa",
            agent_session_id="session-effective-cccc",
            db_connection=self.conn,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["effective"]["agent_session_id"], "session-effective-cccc")

        fetched = get_memory(entity_id=result["data"]["id"], db_connection=self.conn)
        self.assertEqual(fetched["data"]["agent_session_id"], "session-effective-cccc")
        self.assertEqual(fetched["data"]["last_touched_session_id"], "session-effective-cccc")
