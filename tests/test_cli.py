import unittest

from saltmdb.cli import _fmt_digest, _fmt_memory


def _mem(**kwargs) -> dict:
    """Build a minimal search-result memory dict, with sensible defaults."""
    defaults = {
        "id": "abc123",
        "title": "Test Memory",
        "memory_type": "fact",
        "is_core": False,
        "full_content": "Some content.",
        "snippet": "fallback snippet",
    }
    defaults.update(kwargs)
    return defaults


class TestFmtMemory(unittest.TestCase):
    # ------------------------------------------------------------------ structure
    def test_basic_structure(self):
        result = _fmt_memory(_mem())
        self.assertTrue(result.startswith("<memory"), "should open with <memory tag")
        self.assertTrue(result.strip().endswith("</memory>"), "should close with </memory>")
        self.assertIn('id="abc123"', result)
        self.assertIn('type="fact"', result)
        self.assertIn('is_core="false"', result)

    def test_yaml_frontmatter_present(self):
        result = _fmt_memory(_mem())
        self.assertIn("---", result)
        self.assertIn('title: "Test Memory"', result)
        self.assertIn("type: fact", result)
        self.assertIn("is_core: false", result)

    def test_content_body_present(self):
        result = _fmt_memory(_mem(full_content="Hello world"))
        self.assertIn("Hello world", result)

    # ------------------------------------------------------------------ is_core
    def test_is_core_true(self):
        result = _fmt_memory(_mem(is_core=True))
        self.assertIn('is_core="true"', result)
        self.assertIn("is_core: true", result)

    def test_is_core_false(self):
        result = _fmt_memory(_mem(is_core=False))
        self.assertIn('is_core="false"', result)

    # ------------------------------------------------------------------ content precedence
    def test_full_content_preferred_over_snippet(self):
        result = _fmt_memory(_mem(full_content="full body", snippet="snip"))
        self.assertIn("full body", result)
        self.assertNotIn("snip", result)

    def test_falls_back_to_snippet_when_no_full_content(self):
        result = _fmt_memory(_mem(full_content=None, snippet="fallback"))
        self.assertIn("fallback", result)

    def test_falls_back_to_snippet_when_full_content_empty_string(self):
        result = _fmt_memory(_mem(full_content="", snippet="fallback"))
        self.assertIn("fallback", result)

    def test_empty_content_does_not_crash(self):
        result = _fmt_memory(_mem(full_content=None, snippet=None))
        self.assertIn("<memory", result)
        self.assertIn("</memory>", result)

    # ------------------------------------------------------------------ YAML title escaping
    def test_title_double_quote_escaped(self):
        result = _fmt_memory(_mem(title='Say "hello"'))
        self.assertIn('\\"hello\\"', result)

    def test_title_newline_replaced_with_space(self):
        result = _fmt_memory(_mem(title="Line1\nLine2"))
        # The YAML title line must be a single line
        title_line = next(
            (l for l in result.split("\n") if l.startswith("title:")), None
        )
        self.assertIsNotNone(title_line)
        self.assertNotIn("\n", title_line)
        self.assertIn("Line1 Line2", title_line)

    # ------------------------------------------------------------------ XML content escaping
    def test_closing_tag_in_content_is_escaped(self):
        result = _fmt_memory(_mem(full_content="before </memory> after"))
        self.assertNotIn("before </memory> after", result)
        self.assertIn("&lt;/memory&gt;", result)
        # The real closing tag must still be present and unescaped
        self.assertTrue(result.strip().endswith("</memory>"))

    # ------------------------------------------------------------------ missing fields
    def test_missing_memory_type_defaults_to_fact(self):
        m = _mem()
        del m["memory_type"]
        result = _fmt_memory(m)
        self.assertIn('type="fact"', result)

    def test_empty_dict_does_not_crash(self):
        result = _fmt_memory({})
        self.assertIn("<memory", result)
        self.assertIn("</memory>", result)

    # ------------------------------------------------------------------ no section attribute
    def test_no_section_attribute(self):
        result = _fmt_memory(_mem())
        self.assertNotIn("section=", result)


class TestFmtDigest(unittest.TestCase):
    # ------------------------------------------------------------------ wrapper
    def test_wrapper_always_present(self):
        result = _fmt_digest([], [], None)
        self.assertIn("<saltmdb-digest>", result)
        self.assertIn("</saltmdb-digest>", result)

    # ------------------------------------------------------------------ core-rules section
    def test_core_section_present_when_populated(self):
        result = _fmt_digest([_mem()], [], None)
        self.assertIn("<core-rules>", result)
        self.assertIn("</core-rules>", result)

    def test_core_section_absent_when_empty_list(self):
        result = _fmt_digest([], [], None)
        self.assertNotIn("<core-rules>", result)

    def test_core_section_absent_on_error_result(self):
        result = _fmt_digest([{"error": "db failure"}], [], None)
        self.assertNotIn("<core-rules>", result)

    def test_multiple_core_memories_all_rendered(self):
        mems = [_mem(title=f"M{i}", id=f"id{i}") for i in range(3)]
        result = _fmt_digest(mems, [], None)
        for i in range(3):
            self.assertIn(f"id{i}", result)

    # ------------------------------------------------------------------ project-context section
    def test_project_section_present_when_populated_with_keywords(self):
        result = _fmt_digest([], [_mem()], "SALTMDB")
        self.assertIn("<project-context", result)
        self.assertIn('keywords="SALTMDB"', result)
        self.assertIn("</project-context>", result)

    def test_project_section_absent_without_keywords(self):
        result = _fmt_digest([], [_mem()], None)
        self.assertNotIn("<project-context", result)

    def test_project_section_absent_when_empty_list(self):
        result = _fmt_digest([], [], "SALTMDB")
        self.assertNotIn("<project-context", result)

    def test_project_section_absent_when_project_is_empty_simulating_limit_zero(self):
        # When --project-limit 0 is passed, cmd_bootstrap_digest skips the RPC call
        # entirely and leaves project=[], so _fmt_digest must suppress the section.
        result = _fmt_digest([], [], "SALTMDB")
        self.assertNotIn("<project-context", result)
        self.assertIn("<saltmdb-digest>", result)

    def test_project_section_absent_on_error_result(self):
        result = _fmt_digest([], [{"error": "db failure"}], "SALTMDB")
        self.assertNotIn("<project-context", result)

    def test_project_keywords_in_context_tag(self):
        result = _fmt_digest([], [_mem()], "my-project")
        self.assertIn('keywords="my-project"', result)

    # ------------------------------------------------------------------ ordering
    def test_core_rules_before_project_context(self):
        result = _fmt_digest([_mem(id="core-id")], [_mem(id="proj-id")], "kw")
        core_pos = result.index("<core-rules>")
        proj_pos = result.index("<project-context")
        self.assertLess(core_pos, proj_pos)


if __name__ == "__main__":
    unittest.main()
