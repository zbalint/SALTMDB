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


if __name__ == "__main__":
    unittest.main()
