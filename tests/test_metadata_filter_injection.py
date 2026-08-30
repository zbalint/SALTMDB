"""Regression test for the metadata_filter SQL predicate-injection vulnerability.

SALTMDB memory 6ab12268-fc22-45fe-b3b5-30a2fedd9980 / dda53102-b731-43a5-8caf-28e4c2d77235:
`search_memory`'s metadata_filter handling in orchestrator.py f-string-interpolated the
filter dict's *key* straight into a `json_extract(e.metadata, '$.<key>')` SQL predicate,
binding only the value. A key like `safe') OR 1=1 OR json_extract(e.metadata, '$.safe`
injected arbitrary predicate logic and bypassed owner/scope/context filtering entirely.
First reported alpha.72 (2026-08-12), confirmed still open at alpha.102 (2026-08-30).

Fix: metadata_filter keys are now allowlisted against ``^[A-Za-z0-9_]+$`` before
interpolation; anything else raises ValueError instead of reaching the SQL string.
"""

import unittest
import tempfile
import os
import shutil
import json

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import search_memory


class TestMetadataFilterInjection(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

        # Two entities: one owned by "victim" with a secret-ish metadata key/value the
        # attacker (owner_id="attacker") should never be able to see via metadata_filter.
        self._insert_entity("victim-entity", owner_id="victim", metadata={"safe": "no-match"})
        self._insert_entity("attacker-entity", owner_id="attacker", metadata={"safe": "yes"})

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_entity(self, entity_id: str, owner_id: str, metadata: dict) -> None:
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash, memory_type, metadata, scope)"
            " VALUES (?, datetime('now'), datetime('now'), datetime('now'), ?, 'raw',"
            " ?, ?, ?, 'fact', ?, 'private')",
            (entity_id, owner_id, entity_id, f"content for {entity_id}", entity_id,
             json.dumps(metadata)),
        )
        self.conn.commit()

    def test_malicious_key_is_rejected_not_interpolated(self):
        """A metadata_filter key carrying SQL predicate logic must be rejected, not executed.

        search_memory wraps its whole body in a broad except-Exception handler (existing
        contract, see orchestrator.py's final `except Exception as e` block) that turns any
        internal error -- including our ValueError -- into `[{"error": str(e)}]` rather than
        propagating it to the caller. So the observable contract here is an error result, not
        a raised exception; what matters for this regression test is that the malicious key
        never reaches the SQL string.
        """
        malicious_key = "safe') OR 1=1 OR json_extract(e.metadata, '$.safe"
        results = search_memory(
            owner_id="attacker",
            metadata_filter={malicious_key: "no-match"},
            db_path=self.db_path,
            include_related=False,
        )
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])

    def test_injected_filter_cannot_bypass_owner_scope(self):
        """The attacker must never get back the victim's private-scope row by injecting
        `OR 1=1` via the metadata key -- whether that surfaces as an error result or an
        empty list, the victim's row must not be in it."""
        malicious_key = "safe') OR 1=1 OR json_extract(e.metadata, '$.safe"
        results = search_memory(
            owner_id="attacker",
            metadata_filter={malicious_key: "no-match"},
            db_path=self.db_path,
            include_related=False,
        )
        returned_ids = {r["id"] for r in results if "id" in r}
        self.assertNotIn("victim-entity", returned_ids)

    def test_legitimate_metadata_filter_still_works(self):
        """Well-formed keys/values continue to filter correctly (no functional regression)."""
        results = search_memory(
            owner_id="attacker",
            metadata_filter={"safe": "yes"},
            db_path=self.db_path,
            include_related=False,
        )
        returned_ids = {r["id"] for r in results}
        self.assertIn("attacker-entity", returned_ids)
        self.assertNotIn("victim-entity", returned_ids)


if __name__ == "__main__":
    unittest.main()
