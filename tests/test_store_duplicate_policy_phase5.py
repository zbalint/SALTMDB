"""Phase 5 store duplicate policy.

Exact content matches are deterministic rejections; semantic/lexical near matches are advisory
after persistence.  The old token-bearing disposition flow is intentionally not part of this
surface.
"""

import os
import shutil
import tempfile
import unittest
import uuid

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory


class TestStoreDuplicatePolicyPhase5(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "store.db")
        self.conn = init_db(self.db_path)
        self.previous_db_path = os.environ.get("SALTMDB_DB_PATH")
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        if self.previous_db_path is None:
            os.environ.pop("SALTMDB_DB_PATH", None)
        else:
            os.environ["SALTMDB_DB_PATH"] = self.previous_db_path
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _content(self, suffix=""):
        return (
            "# Authentication policy\n\n"
            "OAuth access tokens are short lived and refresh tokens rotate on every use. "
            "API requests carry bearer headers and invalid refresh tokens are rejected. " + suffix
        )

    def test_exact_duplicate_is_structured_hard_failure_with_existing_id(self):
        first = store_memory(
            content=self._content(),
            title="Auth policy A",
            owner_id="agent",
            db_connection=self.conn,
        )
        existing_id = first["data"]["id"]

        duplicate = store_memory(
            content=self._content(),
            title="Auth policy copy",
            owner_id="agent",
            db_connection=self.conn,
        )

        self.assertEqual(duplicate["status"], "rejected")
        self.assertEqual(duplicate["errors"][0]["code"], "REJECT_EXACT_DUPLICATE")
        self.assertIn(existing_id, duplicate["errors"][0]["message"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 1)

    def test_near_duplicate_stores_and_returns_inline_candidates_and_guidance(self):
        store_memory(
            content=self._content(),
            title="Auth policy A",
            owner_id="agent",
            db_connection=self.conn,
        )
        result = store_memory(
            content=self._content(" Access tokens expire after fifteen minutes."),
            title="Auth policy B",
            owner_id="agent",
            db_connection=self.conn,
        )

        self.assertEqual(result["status"], "ok")
        warning = next(item for item in result["warnings"] if item["code"] == "NEAR_DUPLICATE")
        candidates = warning["detail"]["duplicate_candidates"]
        self.assertTrue(candidates)
        self.assertIn("id", candidates[0])
        self.assertIn("title", candidates[0])
        self.assertIn("similarity_score", candidates[0])
        self.assertEqual(warning["detail"]["guidance"]["single"], "supersede_memory")
        self.assertEqual(warning["detail"]["guidance"]["several"], "consolidate_memories")
        # Cold-start review Issue E: duplicate_candidates guidance previously only offered
        # supersede_memory/consolidate_memories, omitting the empirically common third
        # resolution (related but not actually redundant -- link, don't merge/replace).
        self.assertEqual(
            warning["detail"]["guidance"]["related_not_redundant"], "manage_relation"
        )
        self.assertIn("manage_relation", warning["message"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 2)

    def test_fresh_explicit_id_is_preserved_but_cannot_bypass_exact_duplicate_guard(self):
        requested_id = str(uuid.uuid4())
        first = store_memory(
            content=self._content(" Explicit identifier."),
            title="Auth policy explicit",
            owner_id="agent",
            entity_id=requested_id,
            db_connection=self.conn,
        )
        self.assertEqual(first["data"]["id"], requested_id)

        duplicate = store_memory(
            content=self._content(" Explicit identifier."),
            title="Auth policy duplicate",
            owner_id="agent",
            entity_id=str(uuid.uuid4()),
            db_connection=self.conn,
        )
        self.assertEqual(duplicate["status"], "rejected")
        self.assertEqual(duplicate["errors"][0]["code"], "REJECT_EXACT_DUPLICATE")


if __name__ == "__main__":
    unittest.main()
