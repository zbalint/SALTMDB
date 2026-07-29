import unittest
import tempfile
import os
import time
import shutil
from unittest.mock import patch
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory, search_memory
from saltmdb.utils.text import extract_title_and_snippet


class TestQueryCenteredSnippets(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_raw_snippet_sql_centers_on_match_not_document_start(self):
        # Micro sanity check against this exact sqlite build's FTS5 snippet() semantics,
        # independent of the service layer -- pins down that column index 2 is full_content
        # (id=0 UNINDEXED, title=1, full_content=2, search_aliases=3) and that snippet()
        # centers on the match rather than returning the leading text.
        # 80 filler words (well past the 32-token snippet budget) before the rare token,
        # so the excerpt can only contain it if snippet() actually centers on the match
        # instead of just returning the whole (short) document.
        filler = " ".join(f"filler{i}" for i in range(80))
        content = f"{filler} RARETOKENXYZ more filler text here"
        self.conn.execute(
            "INSERT INTO entities_fts(id, title, full_content, search_aliases) VALUES (?, ?, ?, ?)",
            ("e1", "Some Title", content, ""),
        )
        row = self.conn.execute(
            "SELECT snippet(entities_fts, 2, '<mark>', '</mark>', ' ... ', 32) "
            "FROM entities_fts WHERE entities_fts MATCH 'RARETOKENXYZ'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("RARETOKENXYZ", row[0])
        self.assertIn("<mark>", row[0])
        self.assertNotIn("filler0 filler1 filler2", row[0])

    def test_fts_match_returns_query_centered_snippet(self):
        content = (
            "Line one filler filler filler.\n"
            "Line two filler filler filler.\n"
            "Line three filler filler filler.\n"
            "Line four filler filler filler.\n"
            "Line five contains RARETOKENXYZ special marker here.\n"
        )
        res = store_memory(
            title="Buried Token Memory",
            content=content,
            owner_id="user1",
            skip_duplicate_check=True,
            db_path=self.db_path,
        )
        self.assertIn("ID:", res)

        results = search_memory(query_keywords="RARETOKENXYZ", owner_id="user1", db_path=self.db_path)
        self.assertTrue(len(results) > 0)
        top = results[0]

        heuristic_snippet = extract_title_and_snippet(content)[1]
        self.assertNotIn("RARETOKENXYZ", heuristic_snippet)  # confirms the old heuristic misses it

        self.assertIn("RARETOKENXYZ", top["snippet"])
        self.assertIn("<mark>", top["snippet"])
        self.assertNotEqual(top["snippet"], heuristic_snippet)

    def test_semantic_only_match_falls_back_to_heuristic(self):
        content = (
            "This document explains completely different unrelated topics about cooking "
            "recipes and gardening tips for spring season planting."
        )
        res = store_memory(
            title="Unrelated Content Memory",
            content=content,
            owner_id="user1",
            skip_duplicate_check=True,
            db_path=self.db_path,
        )
        entity_id = res.split("ID: ")[1].split()[0]

        with patch(
            "saltmdb.domain.services.memory_service.semantic_search",
            return_value=[(entity_id, 0.05)],
        ):
            results = search_memory(query_keywords="gadgetwidget", owner_id="user1", db_path=self.db_path)

        self.assertTrue(len(results) > 0)
        match = next(r for r in results if r["id"] == entity_id)
        heuristic_snippet = extract_title_and_snippet(content)[1]
        self.assertEqual(match["snippet"], heuristic_snippet)
        self.assertNotIn("<mark>", match["snippet"])

    def test_no_query_listing_uses_heuristic_snippet(self):
        content = "Some plain content for a listing-only search with no query keywords at all."
        store_memory(
            title="Listing Only Memory",
            content=content,
            owner_id="user1",
            skip_duplicate_check=True,
            db_path=self.db_path,
        )

        results = search_memory(owner_id="user1", db_path=self.db_path)
        self.assertTrue(len(results) > 0)
        heuristic_snippet = extract_title_and_snippet(content)[1]
        match = next(r for r in results if r["title"] == "Listing Only Memory")
        self.assertEqual(match["snippet"], heuristic_snippet)


if __name__ == "__main__":
    unittest.main()
