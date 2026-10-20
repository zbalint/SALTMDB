"""Differential coverage for the Markdown header and list-item scanners."""

from __future__ import annotations

import itertools
import random
import re

import pytest

from saltmdb.utils import nlp

# Pre-change implementation: preserve the two replaced patterns as independent oracles.
HEADER = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
LIST = re.compile(r"^[^\S\n]*(?:[\-\*\+]|\d+\.)\s+(.+)$", re.MULTILINE)

_CORPORA = [
    ("#a \n\t", 8),
    ("-1.a \n", 8),
    ("#-٣.a\n\r ", 6),
    ("#*+9.\n \u00a0\u2003a", 6),
    ("1٣.a \n-", 6),
    ("².-a \n", 7),
]
_HANDWRITTEN = [
    "",
    "# First\n### Third",
    "#\n\nTitle",
    "# Base\n####### x",
    "####### x",
    "###### Six",
    "#\n",
    "# \n",
    "#  ",
    "#",
    "\n# #",
    "#\n\nTitle\n## Next",
    "-\n\nitem",
    "- ",
    "-  ",
    "-   ",
    "- \n",
    "- first\nplain\n+ last",
    "\t1. first\n\t* second",
    "٣. item",
    "². x",
    "1- -",
    "#\u00a0\u2003Title\n+\titem",
    "# Title\n```\ncode\n```",
    "# ",
    "#   ",
    "#  \n",
    "#\n\n",
    "#\t\n\n",
    "-\n\nitem\n# Next",
    "\t٣.\n\nlast",
    "-\u001f\u00a0\u2003item",
    "#\r\nTitle\r\n## Next",
]

# Literal dictionaries captured from the unmodified implementation at 3fd3c98,
# before inserting the scanners (the target regexes are unchanged from c3916ab).
_PIPELINE_CASES = [
    (
        "",
        {
            "is_valid": True,
            "header_count": 0,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.0,
        },
    ),
    (
        "# First\n### Third",
        {
            "is_valid": True,
            "header_count": 2,
            "has_header_skip": True,
            "untyped_blocks": 0,
            "msdi": 1.0,
        },
    ),
    (
        "#\n\nTitle",
        {
            "is_valid": True,
            "header_count": 1,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 1.0,
        },
    ),
    (
        "# Base\n####### x",
        {
            "is_valid": True,
            "header_count": 1,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.5,
        },
    ),
    (
        "####### x",
        {
            "is_valid": True,
            "header_count": 0,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.0,
        },
    ),
    (
        "###### Six",
        {
            "is_valid": True,
            "header_count": 1,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 1.0,
        },
    ),
    (
        "#\n",
        {
            "is_valid": True,
            "header_count": 0,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.0,
        },
    ),
    (
        "# \n",
        {
            "is_valid": True,
            "header_count": 0,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.0,
        },
    ),
    (
        "#  ",
        {
            "is_valid": True,
            "header_count": 1,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.0,
        },
    ),
    (
        "#",
        {
            "is_valid": True,
            "header_count": 0,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.0,
        },
    ),
    (
        "\n# #",
        {
            "is_valid": True,
            "header_count": 1,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.0,
        },
    ),
    (
        "#\n\nTitle\n## Next",
        {
            "is_valid": True,
            "header_count": 2,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 1.0,
        },
    ),
    (
        "-\n\nitem",
        {
            "is_valid": True,
            "header_count": 0,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 1.0,
        },
    ),
    (
        "- ",
        {
            "is_valid": True,
            "header_count": 0,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.0,
        },
    ),
    (
        "-  ",
        {
            "is_valid": True,
            "header_count": 0,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.0,
        },
    ),
    (
        "-   ",
        {
            "is_valid": True,
            "header_count": 0,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.0,
        },
    ),
    (
        "- \n",
        {
            "is_valid": True,
            "header_count": 0,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.0,
        },
    ),
    (
        "- first\nplain\n+ last",
        {
            "is_valid": True,
            "header_count": 0,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.667,
        },
    ),
    (
        "\t1. first\n\t* second",
        {
            "is_valid": True,
            "header_count": 0,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.667,
        },
    ),
    (
        "٣. item",
        {
            "is_valid": True,
            "header_count": 0,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.5,
        },
    ),
    (
        "². x",
        {
            "is_valid": True,
            "header_count": 0,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.0,
        },
    ),
    (
        "1- -",
        {
            "is_valid": True,
            "header_count": 0,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.0,
        },
    ),
    (
        "#\u00a0\u2003Title\n+\titem",
        {
            "is_valid": True,
            "header_count": 1,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 1.0,
        },
    ),
    (
        "# Title\n```\ncode\n```",
        {
            "is_valid": True,
            "header_count": 1,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 1.0,
        },
    ),
    (
        "# Real title\n```python\n# comment\n### deeper\n```\nParagraph outside",
        {
            "is_valid": True,
            "header_count": 1,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 0.714,
        },
    ),
    (
        "```text\n- item one\n1. step two\n```\nOutside prose words",
        {
            "is_valid": True,
            "header_count": 0,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 1.111,
        },
    ),
    (
        "# Real title\n```\n# comment\n### deeper\n```\nText\n```\n- item one\n1. step two\n```\nOutside prose words",
        {
            "is_valid": True,
            "header_count": 1,
            "has_header_skip": False,
            "untyped_blocks": 0,
            "msdi": 1.0,
        },
    ),
]


@pytest.mark.parametrize(("alphabet", "max_length"), _CORPORA)
def test_scanners_match_exhaustive_regex_oracles(alphabet, max_length):
    for length in range(max_length + 1):
        for chars in itertools.product(alphabet, repeat=length):
            text = "".join(chars)
            assert nlp._atx_headers(text) == HEADER.findall(text), repr(text)
            assert nlp._list_item_texts(text) == LIST.findall(text), repr(text)


def test_scanners_match_seeded_random_regex_oracles():
    rng = random.Random(8786)
    alphabet = "#-*+1.9٣²a \n\t\r\u00a0\u2003\x1f"
    for _ in range(20_000):
        text = "".join(rng.choices(alphabet, k=rng.randint(1, 200)))
        assert nlp._atx_headers(text) == HEADER.findall(text), repr(text)
        assert nlp._list_item_texts(text) == LIST.findall(text), repr(text)


@pytest.mark.parametrize("text", _HANDWRITTEN)
def test_scanners_match_handwritten_regex_oracles(text):
    assert nlp._atx_headers(text) == HEADER.findall(text)
    assert nlp._list_item_texts(text) == LIST.findall(text)


@pytest.mark.parametrize(("text", "expected"), _PIPELINE_CASES)
def test_validate_markdown_structure_preserves_baseline(text, expected):
    assert nlp.validate_markdown_structure(text) == expected
