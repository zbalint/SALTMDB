import os
import shutil
import tempfile
import unittest

from saltmdb.db.schema import init_db
from saltmdb.mcp import tools
from saltmdb.mcp.identity import SESSION_IDENTITY


class TestStoreContractPhase5(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "store-contract.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self.previous_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self.previous_backend)
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self.conn.close()
        os.environ.pop("SALTMDB_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_yaml_identity_metadata_rejects_with_schema_derived_corrected_call(self):
        result = tools.store_memory(
            title="[Auth] Explicit title",
            tags=["#auth"],
            content=(
                "---\n"
                "title: Hidden title\n"
                "tags: [auth]\n"
                "---\n\n"
                "OAuth refresh tokens rotate after each successful exchange."
            ),
        )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "IDENTITY_IN_YAML_FRONT_MATTER")
        corrected = result["corrected_call"]
        self.assertEqual(corrected["title"], "[Auth] Explicit title")
        self.assertEqual(corrected["tags"], ["#auth"])
        self.assertFalse(corrected["content"].startswith("---"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 0)

    def test_success_reports_submitted_and_effective_tags_and_near_miss_warning(self):
        first = tools.store_memory(
            title="[Docs] Canonical seed",
            tags=["#document"],
            content="Documentation conventions require bracketed headings and explicit examples.",
        )
        self.assertEqual(first["status"], "ok")

        result = tools.store_memory(
            title="[Docs] Plural spelling",
            tags=["#documents"],
            content="Documentation reviews verify headings, examples, and complete correction guidance.",
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["submitted_tags"], ["#documents"])
        self.assertIn("#document", result["data"]["effective_tags"])
        self.assertTrue(any(item["code"] == "TAG_NEAR_MISS" for item in result["warnings"]))
        self.assertEqual(result["effective"]["owner_id"], "test_agent")

    def test_store_memory_rejects_adjacent_separator_tag_before_any_write(self):
        result = tools.store_memory(
            title="[Test] Adjacent separator tag",
            tags=["#foo::bar"],
            content="Some sufficiently long content body for the quality gate to accept without issue here.",
        )

        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error: "))
        self.assertIn("adjacent", result.lower())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 0)

    def test_store_memory_collapses_single_separator_tag_and_still_warns_near_miss(self):
        result = tools.store_memory(
            title="[Test] Single separator tag",
            tags=["#wayfinder:task"],
            content="Some sufficiently long content body for the quality gate to accept without issue here.",
        )

        self.assertEqual(result["status"], "ok")
        self.assertIn("#wayfinder-task", result["data"]["effective_tags"])
        self.assertTrue(any(item["code"] == "TAG_NEAR_MISS" for item in result["warnings"]))

    def test_revise_memory_rejects_adjacent_separator_tag(self):
        stored = tools.store_memory(
            title="[Test] Revision source",
            tags=["#testing"],
            content="Some sufficiently long source content body for the quality gate to accept without issue here.",
        )
        self.assertEqual(stored["status"], "ok")

        result = tools.revise_memory(
            entity_id=stored["data"]["id"],
            title="[Test] Revised",
            content="Different content body long enough to pass quality checks easily.",
            tags=["#foo::bar"],
            reason="testing adjacent separator rejection",
        )

        self.assertIsInstance(result, dict)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("adjacent", result["errors"][0]["message"].lower())

    def test_consolidate_memories_rejects_adjacent_separator_tag(self):
        first = tools.store_memory(
            title="[Test] Related parent A",
            tags=["#testing"],
            content="Parent A content for a closely related consolidation tag validation test.",
        )
        second = tools.store_memory(
            title="[Test] Related parent B",
            tags=["#testing"],
            content="Parent B content, closely related, for the same consolidation tag validation test.",
        )
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "ok")

        result = tools.consolidate_memories(
            parent_ids=[first["data"]["id"], second["data"]["id"]],
            title="[Test] Consolidated",
            content="Consolidated content body long enough to pass quality checks easily.",
            tags=["#foo::bar"],
        )

        self.assertIsInstance(result, dict)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("adjacent", result["errors"][0]["message"].lower())


if __name__ == "__main__":
    unittest.main()
