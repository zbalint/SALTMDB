"""Differential coverage for the Markdown code-fence scanner."""

from __future__ import annotations

import itertools
import random
import re

from saltmdb.utils import nlp

# Pre-change implementation: these are the three regexes replaced by the scanner.
P282 = re.compile(r"```[\s\S]*?(?:```|\Z)")
PCLOSED = re.compile(r"```[\s\S]*?```")

_ALPHABET = "`a\n "

_HANDWRITTEN = [
    "",
    "# Orchard\n\nBefore words.\n```python\n### Hidden heading\nvalue = 7\n```\n## Harvest\n- Pick apples\nAfter words.",
    "# Orchard\n```\n\nvalue = 7\n```\nPlain words.",
    "# Orchard\nUse `small code` with plain words.",
    "Before```python\nx\n```Between```unfinished\n### Visible heading",
    "```python\nx\n```Middle```text\ny\n```",
    "```",
    "left```x```right",
    "# Orchard\n```text\n## Hidden\n```\n### Harvest\nPlain words.",
    "# Orchard\n```python\n### Hidden heading",
    "Intro ```python\ncode words\n# Title\n### Skipped",
    "```python\nx = 1\n```\nIntro ```javascript\ncode words\n# Title\n### Skipped",
    "# Title\nIntro ```python\nhidden words\nAfter words remain outside",
]
_HANDWRITTEN.extend("left" + "`" * count + "right" for count in range(3, 10))

_EXPECTED_EXTRACT = [
    "",
    "# Orchard Before words. ## Harvest - Pick apples After words.",
    "# Orchard Plain words.",
    "# Orchard Use with plain words.",
    "Before Between",
    "Middle",
    "",
    "left right",
    "# Orchard ### Harvest Plain words.",
    "# Orchard",
    "Intro",
    "Intro",
    "# Title Intro",
    "left",
    "left",
    "left",
    "left right",
    "left `right",
    "left ``right",
    "left",
]
_EXPECTED_VALIDATE = [
    {
        "is_valid": True,
        "header_count": 0,
        "has_header_skip": False,
        "untyped_blocks": 0,
        "msdi": 0.0,
    },
    {
        "is_valid": True,
        "header_count": 2,
        "has_header_skip": False,
        "untyped_blocks": 0,
        "msdi": 0.692,
    },
    {
        "is_valid": True,
        "header_count": 1,
        "has_header_skip": False,
        "untyped_blocks": 0,
        "msdi": 0.6,
    },
    {
        "is_valid": True,
        "header_count": 1,
        "has_header_skip": False,
        "untyped_blocks": 0,
        "msdi": 0.143,
    },
    {
        "is_valid": False,
        "error_flag": "BROKEN_MARKDOWN_SYNTAX",
        "reason": "Unclosed Markdown code block detected (odd count of ``` markers).",
    },
    {
        "is_valid": False,
        "error_flag": "BROKEN_MARKDOWN_SYNTAX",
        "reason": "Unclosed Markdown code block detected (odd count of ``` markers).",
    },
    {
        "is_valid": False,
        "error_flag": "BROKEN_MARKDOWN_SYNTAX",
        "reason": "Unclosed Markdown code block detected (odd count of ``` markers).",
    },
    {
        "is_valid": True,
        "header_count": 0,
        "has_header_skip": False,
        "untyped_blocks": 0,
        "msdi": 0.333,
    },
    {
        "is_valid": True,
        "header_count": 2,
        "has_header_skip": True,
        "untyped_blocks": 0,
        "msdi": 0.667,
    },
    {
        "is_valid": False,
        "error_flag": "BROKEN_MARKDOWN_SYNTAX",
        "reason": "Unclosed Markdown code block detected (odd count of ``` markers).",
    },
    {
        "is_valid": True,
        "header_count": 2,
        "has_header_skip": True,
        "untyped_blocks": 0,
        "msdi": 0.333,
    },
    {
        "is_valid": True,
        "header_count": 2,
        "has_header_skip": True,
        "untyped_blocks": 0,
        "msdi": 0.556,
    },
    {
        "is_valid": True,
        "header_count": 1,
        "has_header_skip": False,
        "untyped_blocks": 0,
        "msdi": 0.111,
    },
    {
        "is_valid": True,
        "header_count": 0,
        "has_header_skip": False,
        "untyped_blocks": 0,
        "msdi": 0.0,
    },
    {
        "is_valid": True,
        "header_count": 0,
        "has_header_skip": False,
        "untyped_blocks": 0,
        "msdi": 0.0,
    },
    {
        "is_valid": True,
        "header_count": 0,
        "has_header_skip": False,
        "untyped_blocks": 0,
        "msdi": 0.0,
    },
    {
        "is_valid": True,
        "header_count": 0,
        "has_header_skip": False,
        "untyped_blocks": 0,
        "msdi": 0.0,
    },
    {
        "is_valid": True,
        "header_count": 0,
        "has_header_skip": False,
        "untyped_blocks": 0,
        "msdi": 0.0,
    },
    {
        "is_valid": True,
        "header_count": 0,
        "has_header_skip": False,
        "untyped_blocks": 0,
        "msdi": 0.0,
    },
    {
        "is_valid": True,
        "header_count": 0,
        "has_header_skip": False,
        "untyped_blocks": 0,
        "msdi": 0.0,
    },
]


def _cases() -> list[str]:
    cases = [
        "".join(chars)
        for length in range(10)
        for chars in itertools.product(_ALPHABET, repeat=length)
    ]
    rng = random.Random(20261020)
    for _ in range(20_000):
        length = rng.randint(10, 400)
        cases.append(
            "".join("`" if rng.random() < 0.5 else rng.choice("a\n ") for _ in range(length))
        )
    cases.extend(_HANDWRITTEN)
    return cases


def test_fence_helpers_match_old_regexes_exhaustive_and_seeded_random() -> None:
    for text in _cases():
        assert P282.sub(" ", text) == nlp._replace_code_fences(text, " ", unclosed_to_end=True)
        assert PCLOSED.sub("", text) == nlp._replace_code_fences(text, "")
        assert PCLOSED.findall(text) == [
            text[start:end] for start, end in nlp._code_fence_spans(text)
        ]


def test_fence_helpers_match_old_regexes_on_handwritten_fixtures() -> None:
    for text in _HANDWRITTEN:
        assert P282.sub(" ", text) == nlp._replace_code_fences(text, " ", unclosed_to_end=True)
        assert PCLOSED.sub("", text) == nlp._replace_code_fences(text, "")
        assert PCLOSED.findall(text) == [
            text[start:end] for start, end in nlp._code_fence_spans(text)
        ]


def test_real_nlp_functions_keep_literal_old_outputs() -> None:
    for text, expected_extract, expected_validate in zip(
        _HANDWRITTEN, _EXPECTED_EXTRACT, _EXPECTED_VALIDATE, strict=True
    ):
        assert nlp.extract_prose_content(text) == expected_extract
        assert nlp.validate_markdown_structure(text) == expected_validate
