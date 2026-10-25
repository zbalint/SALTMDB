import os
import re
import tempfile
import unittest

from hypothesis import example, given, strategies as st

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service.search_primitives import _run_fts_search
from saltmdb.utils.text import sanitize_fts_query


TOKEN_WORDS = [
    "AND",
    "OR",
    "NOT",
    "NEAR",
    "foo",
    '"',
    "(",
    ")",
    "*",
    "^",
    ":",
    "-",
    "+",
    ",",
    ".",
    "'",
    "_",
    "é",
    "٠",
    "á",
    "😀",
    "​",
]
query_text = st.lists(
    st.one_of(st.text(), st.sampled_from(TOKEN_WORDS)),
    max_size=20,
).map(" ".join)


class TestFtsQueryFuzz(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp_dir = tempfile.TemporaryDirectory()
        cls.conn = init_db(os.path.join(cls._temp_dir.name, "fts.sqlite"))
        cls.conn.execute(
            "INSERT INTO entities(id, title, full_content, status, weight, is_core, created_at, "
            "updated_at, last_accessed_at, agent_id, scope, metadata, memory_type) "
            "VALUES (?, ?, ?, ?, 1, 0, datetime('now'), datetime('now'), datetime('now'), "
            "'test', 'shared', '{}', 'fact')",
            ("fuzz-row", "Fuzz row", "foo bar operator words", "raw"),
        )
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls._temp_dir.cleanup()

    @given(query_text)
    @example("OR")
    @example("foo AND")
    @example('"')
    def test_q1_composed_fts_queries_complete(self, text: str):
        sanitized = sanitize_fts_query(text)
        and_rows = _run_fts_search(self.conn, sanitized, [], [], 5, 0)
        or_rows = _run_fts_search(self.conn, sanitized, [], [], 5, 0, or_only=True)
        self.assertIsInstance(and_rows, list)
        self.assertIsInstance(or_rows, list)

    @given(st.text())
    @example("a, b")
    @example(" x")
    @example('a"b')
    def test_q2_sanitized_output_is_word_terms(self, text: str):
        self.assertIsNotNone(re.fullmatch(r"(\w+( \w+)*)?", sanitize_fts_query(text)))

    def test_q3_preserves_uppercase_operator(self):
        self.assertEqual(sanitize_fts_query("foo OR"), "foo OR")
