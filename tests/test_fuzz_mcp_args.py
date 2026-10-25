import json
import re
import unittest

from hypothesis import example, given, strategies as st

from saltmdb.mcp.tools import _normalize_list_or_str
from saltmdb.utils.pagination import cursor_offset


mcp_text = st.one_of(
    st.text(max_size=40),
    st.text(alphabet=st.sampled_from('[] ,\\9-"'), max_size=40),
)
hostile_mcp_input = st.one_of(
    mcp_text,
    st.integers(min_value=4290, max_value=6000).map(lambda digits: "[" + "9" * digits + "]"),
    st.integers(min_value=2000, max_value=400000).map(lambda depth: "[" * depth + "]" * depth),
    st.lists(st.integers(), max_size=20).map(json.dumps),
)

cursor_pieces = st.sampled_from(
    (
        "offset:",
        "0",
        "1",
        "9",
        "٣",
        "٣٣",
        "+",
        "-",
        " ",
        "\n",
    )
)
cursor_strings = st.one_of(
    st.lists(cursor_pieces, min_size=0, max_size=8).map("".join),
    st.integers(min_value=0, max_value=10**6).map(lambda n: f"offset:{n}"),
    st.integers(min_value=1, max_value=5000).map(lambda n: "offset:" + "9" * n),
)


class TestFuzzMcpArgs(unittest.TestCase):
    @given(hostile_mcp_input)
    @example("[" * 200000 + "]" * 200000)
    @example("[" + "9" * 5000 + "]")
    def test_m1_hostile_list_text_returns_a_list(self, text: str):
        result = _normalize_list_or_str(text)
        self.assertIsInstance(result, list)

    @staticmethod
    def _cursor_oracle(cursor: object, max_offset: int) -> tuple[int, str | None]:
        if cursor is None:
            return 0, None
        if not isinstance(cursor, str):
            return 0, "cursor must be a string."
        maximum = str(max_offset)
        match = re.fullmatch(r"offset:(0|[1-9][0-9]*)", cursor, re.ASCII)
        if match is None:
            return 0, f"cursor must match offset:<0..{max_offset}>."
        digits = match.group(1)
        if len(digits) > len(maximum):
            return 0, f"cursor must match offset:<0..{max_offset}>."
        if len(digits) == len(maximum) and digits > maximum:
            return 0, f"cursor offset must be at most {max_offset}."
        return int(digits), None

    @given(cursor_strings, st.integers(min_value=0, max_value=10**6))
    @example("offset:1٣", 10000)
    @example("offset:٣", 10000)
    @example("offset:" + "9" * 5000, 10000)
    @example("offset:007", 10000)
    @example("offset:10000", 10000)
    @example("offset:10000\n", 10000)
    @example("offset:0", 0)
    @example("offset:10001", 10000)
    def test_m2_cursor_offset_matches_ascii_oracle(self, cursor: str, max_offset: int):
        self.assertEqual(cursor_offset(cursor, max_offset), self._cursor_oracle(cursor, max_offset))

    @given(st.one_of(st.just(b"offset:1"), st.just(1), st.just(None)), st.just(10))
    @example(b"offset:1", 10)
    @example(1, 10)
    @example(None, 10)
    def test_m2_non_string_cursor_cases_are_pinned(self, cursor: object, max_offset: int):
        self.assertEqual(cursor_offset(cursor, max_offset), self._cursor_oracle(cursor, max_offset))


if __name__ == "__main__":
    unittest.main()
