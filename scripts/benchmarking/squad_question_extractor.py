"""Safe extraction of SQuAD `question:` text from raw `test_data/squad/**/*.md` frontmatter, for
the precision-first search evaluation (`scratch/plans/precision_first_search_evaluation.md`,
§0b item 14 / §2c).

Why this exists: `build_diverse_test_db.py`'s own frontmatter allowlist parses `title`/`url`/
`label`/`source_dataset` but never `question` or `answers` -- squad's frozen entities in
`scratch/diverse_corpus_full.db` therefore carry only a generic `[squad] {id}` title and the bare
passage body, not the question. This module recovers `question:` from the RAW source file (path
derivable from a frozen entity's `metadata.source_relpath`), using the exact same discipline
`build_diverse_test_db.py`'s `parse_frontmatter_file` already established: **line-based scanning
only, never a real YAML/pickle load** -- squad's `answers:` field is a
`!!python/object/apply:numpy...` pickle tag sitting in the same frontmatter block that a real
YAML loader would choke on or, worse, execute. This module never touches that field at all; it
only needs to locate the right *entity*, not reproduce squad's exact answer span.

Handles YAML plain-scalar line-folding (a `question:` value can wrap across multiple physical
lines, continuation lines indented, folded into a single string) AND double-quoted-scalar
`\\xHH` hex escapes / `\\ ` backslash-space line-continuation artifacts that appear in some squad
questions containing non-ASCII characters (verified against real files, e.g. "Beyoncé"/
"Frédéric" render as `\\xE9`-escaped sequences in the raw double-quoted YAML value). Verified
against a random sample of 300 of the corpus's 87,599 raw squad files this session: 0 failed
extractions, 0 residual escape artifacts.
"""

import re
from pathlib import Path

# Same key-token shape as build_diverse_test_db.py's FRONTMATTER_KEY_RE, used here only to detect
# "this line starts a NEW top-level key" (i.e. where a wrapped question: value ends), not to
# extract other fields.
_KEY_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)$")
_HEX_ESCAPE_RE = re.compile(r"\\x([0-9A-Fa-f]{2})")
_BACKSLASH_SPACE_RE = re.compile(r"\\+\s+")


def _clean_scalar(raw: str) -> str:
    """Decodes \\xHH hex escapes, collapses backslash-space line-continuation artifacts, unescapes
    \\", normalizes whitespace, and strips one layer of surrounding quotes if present. Not a full
    YAML unescaper (doesn't need to be -- see module docstring) -- verified sufficient against a
    300-file random sample, 0 residual artifacts."""
    s = raw
    s = _HEX_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), s)
    s = _BACKSLASH_SPACE_RE.sub(" ", s)
    s = s.replace('\\"', '"')
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1]
    return s.strip()


def extract_question(path: Path) -> str | None:
    """Returns the cleaned `question:` value from a raw squad frontmatter file, or None if the
    file doesn't have the expected `---`-delimited frontmatter shape or no `question` key.
    Line-scan only -- see module docstring for why this must never become a real YAML load."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return None
    if not lines or lines[0].strip() != "---":
        return None
    close_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close_idx = i
            break
    if close_idx is None:
        return None

    frontmatter = lines[1:close_idx]
    question: str | None = None
    i = 0
    while i < len(frontmatter):
        m = _KEY_RE.match(frontmatter[i])
        if m and m.group(1) == "question":
            parts = [m.group(2)]
            j = i + 1
            # Continuation lines: anything that does NOT itself look like a new top-level key.
            while j < len(frontmatter) and not _KEY_RE.match(frontmatter[j]):
                parts.append(frontmatter[j])
                j += 1
            question = _clean_scalar(" ".join(parts))
            i = j
            continue
        i += 1
    return question


def squad_entity_source_path(test_data_root: Path, source_relpath: str) -> Path:
    """Given a frozen entity's `metadata.source_relpath` (e.g.
    "squad/chunk_431/doc_431751_part_000.md"), returns the raw file path under test_data/."""
    return test_data_root / source_relpath
