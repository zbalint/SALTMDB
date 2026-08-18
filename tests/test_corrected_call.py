import unittest

from saltmdb.utils.corrected_call import build_corrected_call, missing_required_fields
from _test_helpers import assert_corrected_call_complete


def _example_tool(
    title: str, content: str, tags: list, owner_id: str = None, scope: str = "shared"
):
    """Stand-in for a Phase-2-shaped @mcp.tool() function: required title/content/tags, optional
    owner_id/scope. Never called -- only its signature is introspected."""
    raise NotImplementedError


class TestBuildCorrectedCall(unittest.TestCase):
    def test_fix_overrides_submitted(self):
        submitted = {"title": "Background", "content": "c", "tags": ["#x"]}
        fixes = {"title": "[Proj] Better Title"}
        corrected = build_corrected_call(_example_tool, submitted, fixes)
        self.assertEqual(corrected["title"], "[Proj] Better Title")
        self.assertEqual(corrected["content"], "c")
        self.assertEqual(corrected["tags"], ["#x"])

    def test_unknown_keys_dropped(self):
        submitted = {"title": "T", "content": "c", "tags": ["#x"], "bogus_alias": "value"}
        corrected = build_corrected_call(_example_tool, submitted, {})
        self.assertNotIn("bogus_alias", corrected)

    def test_none_values_omitted(self):
        submitted = {"title": "T", "content": "c", "tags": ["#x"], "owner_id": None}
        corrected = build_corrected_call(_example_tool, submitted, {})
        self.assertNotIn("owner_id", corrected)

    def test_required_field_supplied_only_by_fix(self):
        submitted = {"content": "c", "tags": ["#x"]}
        fixes = {"title": "[Proj] Extracted From Front Matter"}
        corrected = build_corrected_call(_example_tool, submitted, fixes)
        self.assertEqual(corrected["title"], "[Proj] Extracted From Front Matter")
        self.assertEqual(missing_required_fields(_example_tool, corrected), [])

    def test_missing_required_fields_reports_gap(self):
        corrected = {"content": "c"}  # title, tags still missing
        missing = missing_required_fields(_example_tool, corrected)
        self.assertEqual(set(missing), {"title", "tags"})

    def test_invariant_harness_passes_on_complete_call(self):
        submitted = {"content": "c", "tags": ["#x"]}
        corrected = build_corrected_call(_example_tool, submitted, {"title": "[Proj] Fixed"})
        assert_corrected_call_complete(self, _example_tool, corrected)

    def test_invariant_harness_fails_on_incomplete_call(self):
        with self.assertRaises(AssertionError):
            assert_corrected_call_complete(self, _example_tool, {"content": "c"})

    def test_var_kwargs_never_leak_through(self):
        def legacy_tool(title: str, **kwargs):
            raise NotImplementedError

        corrected = build_corrected_call(legacy_tool, {"title": "T", "kwargs": {"x": 1}}, {})
        self.assertEqual(corrected, {"title": "T"})


if __name__ == "__main__":
    unittest.main()
