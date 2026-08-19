import unittest
from unittest.mock import patch

from saltmdb.daemon.dispatch import (
    _dispatch_commit_consolidation,
    _dispatch_get_lineage,
    _dispatch_get_memory,
    _dispatch_get_related_memories,
    _dispatch_search_memory,
    _dispatch_store_memory,
)


class TestDispatchRequestDefaults(unittest.TestCase):
    @patch("saltmdb.daemon.dispatch.memory_service.store_memory", return_value="stored")
    def test_store_omitted_options_match_service_defaults(self, store):
        _dispatch_store_memory(content="valid content", owner_id="owner", title="A title")
        call = store.call_args.kwargs
        self.assertEqual(call["scope"], "shared")
        self.assertEqual(call["weight"], 1)
        self.assertNotIn("skip_duplicate_check", call)

    @patch("saltmdb.daemon.dispatch.memory_service.search_memory", return_value=[])
    def test_search_omitted_options_match_service_defaults(self, search):
        _dispatch_search_memory(owner_id="owner", query_keywords="query")
        call = search.call_args.kwargs
        self.assertFalse(call["explain_mode"])
        self.assertEqual(call["limit"], 5)
        self.assertEqual(call["tag_operator"], "AND")
        self.assertEqual(call["mode"], "broad")
        self.assertTrue(call["include_related"])
        self.assertFalse(call["prefer_durable_types"])
        self.assertFalse(call["demote_superseded"])

    def test_consolidation_requires_title_and_content(self):
        with self.assertRaisesRegex(ValueError, "title is required"):
            _dispatch_commit_consolidation(parent_ids=["parent"], content="content")
        with self.assertRaisesRegex(ValueError, "content is required"):
            _dispatch_commit_consolidation(parent_ids=["parent"], title="title")

    @patch("saltmdb.daemon.dispatch.memory_service.get_memory", return_value="content")
    def test_get_memory_uses_explicit_id_fetch(self, fetch):
        self.assertEqual(_dispatch_get_memory(entity_id="entity-id"), "content")
        fetch.assert_called_once_with(entity_id="entity-id")

    @patch("saltmdb.daemon.dispatch.relation_service.get_lineage", return_value={"nodes": []})
    def test_get_lineage_defaults_to_ancestor_traversal(self, lineage):
        _dispatch_get_lineage(entity_id="entity-id")
        lineage.assert_called_once_with(entity_id="entity-id", direction="ancestors", max_depth=5)

    @patch(
        "saltmdb.daemon.dispatch.relation_service.get_related_memories",
        return_value={"dependencies": []},
    )
    def test_get_related_memories_delegates_dependency_traversal(self, related):
        _dispatch_get_related_memories(entity_id="entity-id", max_depth=3)
        related.assert_called_once_with(entity_id="entity-id", max_depth=3)


if __name__ == "__main__":
    unittest.main()
