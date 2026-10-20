# SPEC-SONAR-REGEX-SCANNERS

## 0. Status

LOCKED 2026-10-20

- Backlog item: none. Sonar rule `python:S8786` (2 issues) on `develop` `c3916ab`, in `src/saltmdb/utils/nlp.py`: line 453 `headers = re.findall(r"^(#{1,6})\s+(.+)$", text_no_code, re.MULTILINE)` (in `_markdown_headers`) and line 468 `list_items = re.findall(r"^[^\S\n]*(?:[\-\*\+]|\d+\.)\s+(.+)$", text, re.MULTILINE)` (in `_markdown_metrics`). Both lines were read at `c3916ab` and match the issue messages and ranges. (An earlier spec mistook these for the fence patterns; see the context event "Mistake found after pushing SPEC-SONAR-FENCE-SCANNER".) Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` at `77e18db` or later; `./verify` exits 0 (2948 passed, 1 skipped, 233 subtests; `tests/test_adapter_signal_shutdown.py` is a known load-sensitive test, rerun once in isolation before calling a failure real).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the helper functions directly (differential tests) and `validate_markdown_structure` through fixtures with literal expected values.
- Scope (may edit): `src/saltmdb/utils/nlp.py` (the two call sites and the new helpers of D1) and the new test file `tests/test_nlp_markdown_scanners.py`.
- Does not touch: any other regex, the fence helpers, thresholds, quality-gate logic, any existing test, any other file.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Both regexes end in `\s+(.+)$`: `\s` and `.` overlap, so Sonar reports super-linear backtracking. The semantics are subtle, not a plain "marker, spaces, text": `\s+` also matches newlines, so a marker followed by blank lines takes the NEXT non-blank text as its content (for example `#\n\nTitle` is the header `Title`), and a marker followed only by whitespace to the end of the text can still match by letting `.+` take one of the whitespace characters. Any rewrite must keep this exactly; a simplified regex (`[^\S\n]+(\S.*)`) would change header counts and the header-skip result of the quality gate, which sits on the write path. The architect therefore prototyped exact line scanners and compared them with the two regexes: 0 disagreements over about 62 million inputs (exhaustive strings up to length 7 to 9 over five alphabets including carriage return, tab, a no-break space, an em space, `\x1f`, ASCII and Arabic-Indic digits, plus 160,000 seeded random strings of length up to 200). The reference code of D1 is that prototype. Rejected: a simplified regex or possessive/atomic forms (behaviour change; possessive needs Python 3.11 and `pyproject.toml` requires 3.10), any suppression or issue-status change (owner rules).

## 2. Decisions

- D1. Add these private helpers to `nlp.py`, above `_markdown_headers`, exactly as the reference code below (names, signatures and logic; comments may be shortened but not the logic). Python `\s` equals `str.isspace()`, `\d` equals `str.isdecimal()`, and MULTILINE `^` means "offset 0 or after a newline".

```python
def _line_end(text, pos):
    end = text.find("\n", pos)
    return len(text) if end == -1 else end


def _tail_after_marker(text, pos):
    """Emulate `\\s+(.+)$` (MULTILINE) starting at pos; return (group_start, match_end) or None."""
    n = len(text)
    run_end = pos
    while run_end < n and text[run_end].isspace():
        run_end += 1
    if run_end == pos:
        return None
    if run_end < n:
        return run_end, _line_end(text, run_end)
    # whitespace to the end of text: `.+` must take a non-newline character of the run
    k = run_end - 1
    while k > pos and text[k] == "\n":
        k -= 1
    if k == pos:
        return None
    return k, _line_end(text, k)


def _scan_line_matches(text, match_at):
    """Emulate re.findall with a MULTILINE `^...` pattern: match_at(text, start) -> (record, end) | None."""
    n = len(text)
    out = []
    pos = 0
    while pos <= n:
        if pos != 0 and text[pos - 1] != "\n":
            nl = text.find("\n", pos)
            if nl == -1:
                break
            pos = nl + 1
        hit = match_at(text, pos)
        if hit is None:
            pos += 1
        else:
            record, pos = hit
            out.append(record)
    return out


def _atx_header_at(text, start):
    n = len(text)
    q = start
    while q < n and q - start < 6 and text[q] == "#":
        q += 1
    if q == start:
        return None
    tail = _tail_after_marker(text, q)
    if tail is None:
        return None
    g2, end = tail
    return (text[start:q], text[g2:end]), end


def _ordered_marker_end(text, pos):
    """End offset of `\\d+\\.` at pos, else None."""
    n = len(text)
    d = pos
    while d < n and text[d].isdecimal():
        d += 1
    if d == pos or d >= n or text[d] != ".":
        return None
    return d + 1


def _list_item_at(text, start):
    n = len(text)
    q = start
    while q < n and text[q].isspace() and text[q] != "\n":
        q += 1
    if q >= n:
        return None
    if text[q] in "-*+":
        q += 1
    else:
        q = _ordered_marker_end(text, q)
        if q is None:
            return None
    tail = _tail_after_marker(text, q)
    if tail is None:
        return None
    g1, end = tail
    return text[g1:end], end


def _atx_headers(text):
    return _scan_line_matches(text, _atx_header_at)


def _list_item_texts(text):
    return _scan_line_matches(text, _list_item_at)
```

- D2. `_markdown_headers` (line 453 at `c3916ab`): `headers = _atx_headers(text_no_code)`. The result is a list of `(hashes, title)` string pairs, exactly what `re.findall` returned with two groups. The following lines of the function are unchanged.
- D3. `_markdown_metrics` (line 468): `list_items = _list_item_texts(text)`, a list of strings, exactly what `re.findall` returned with one group. The other lines are unchanged.
- D4. Each new function reaches `complexipy` score at most 12 (proxy command in section 6); the consultant's hand estimate puts `_list_item_at` near the limit, which is why D1 already splits the ordered-number branch into `_ordered_marker_end`. If any function still cannot, split it further; never raise the limit.
- D5. The new test file `tests/test_nlp_markdown_scanners.py` keeps the two OLD patterns as oracles (verbatim module constants, commented as the pre-change implementation) and compares `HEADER.findall(t) == _atx_headers(t)` and `LIST.findall(t) == _list_item_texts(t)` for: (1) every string over the alphabets `#a<space>\n\t` and `-1.a<space>\n` up to length 8 (488,281 strings each), over `#-U+0663.a\n\r<space>`, `#*+9.\n U+00A0 U+2003 a` and `1U+0663.a<space>\n-` up to length 6, and over `U+00B2.-a<space>\n` up to length 7 (U+00B2 is `isdigit()` but not `isdecimal()`, so it separates `\d` from `isdigit`) (the whole test must stay under about 20 seconds; if it does not, reduce the length and amend the spec); (2) a seeded random set (`random.Random(seed)`, at least 20,000 strings of length 1 to 200 over `#-*+1.9`, U+0663, U+00B2, `a`, space, `\n\t\r`, U+00A0, U+2003 and `\x1f`); (3) hand-written fixtures including: blank lines between a marker and its text, a marker followed only by whitespace to the end (with one, two and three trailing characters and with trailing newlines), seven hashes followed by a space and text (`####### x`: no header), six hashes, a header directly after a multi-line match, a list item at offset 0 and at the end, ordered and bullet lists with leading tabs, an Arabic-Indic number, and `² x` after a dot (`². x`: no list item, because `\d` does not match U+00B2). For the real pipeline: through `validate_markdown_structure` on at least ten of those fixtures, assert LITERAL expected dictionaries (`header_count`, `has_header_skip`, `untyped_blocks`, `msdi`; at least three of them header-positive: one with a level skip (`#` then `###`), one `#\n\nTitle`, and one with seven hashes; no existing test pins a positive `header_count` or `has_header_skip`) captured by running the UNMODIFIED code at `c3916ab` before editing `nlp.py`. Corpus strings are generated or fictional; no memory content.
- D6. No new Sonar finding: no regex with a lazy quantifier or overlapping classes, no nested ternary, no unused local. `ruff check --select ARG,F,RET` on `nlp.py` reports nothing the baseline does not.

## 3. Changes per file

| File | Change |
|---|---|
| `src/saltmdb/utils/nlp.py` | helpers of D1; the two call sites (D2, D3) |
| `tests/test_nlp_markdown_scanners.py` | new (D5) |

## 4. Tests

The new file only; no existing test changes. The oracle constants and the literal fixture values are the pre-change behaviour; the file fails to import or call on the unmodified source (the helpers do not exist) and on each mutant of section 8.4.

## 5. Documentation

None.

## 6. Verification procedure

`./verify`; the new test file in isolation; `ruff check --select ARG,F,RET src/saltmdb/utils/nlp.py` against the baseline copy; the complexity proxy (outside the repo, never added to `pyproject.toml` or `uv.lock`):

```sh
SCRATCH=<a scratch directory you create under /tmp>; echo "$SCRATCH"
uv venv -q "$SCRATCH/cxvenv" && uv pip install -q --python "$SCRATCH/cxvenv/bin/python" complexipy
"$SCRATCH/cxvenv/bin/complexipy" --max-complexity-allowed 12 src/saltmdb/utils/nlp.py
```

and a timing probe: `_atx_headers` and `_list_item_texts` on texts of 20,000, 40,000 and 80,000 repetitions of `"#\n"`, `"# a\n"`, `"-" + " " * 5 + "\n"`, `"#" + " " * n` and a blank-line run `"\n" * n + "- x"` (the only check that catches a quadratic leading-space loop) stay linear (below 100 ms at 80,000 repeats, growth about 2x per doubling).

## 7. Out of scope

Every other regex in `nlp.py`, the fence helpers, thresholds.

## 8. Acceptance

1. `./verify` exits 0.
2. `git status` lists only `src/saltmdb/utils/nlp.py` and the new test file (the second as untracked).
3. The differential tests cover the corpora of D5 and pass; the oracles are the old patterns.
4. Mutation sample on a scratch copy (repo tests as killers; the tester names them): in `_tail_after_marker` change `k > pos` to `k >= pos` (killed by `#\n`), drop the `while k > pos and text[k] == "\n"` loop (`# \n`), return `run_end` instead of `k` in the all-whitespace branch (`#  `), change `run_end == pos` to `run_end < pos` (`#`); in `_atx_header_at` change `q - start < 6` to `<= 6` (killed only by the fixture `####### x`; the exhaustive alphabets do not reach it); in `_ordered_marker_end` replace `isdecimal()` by `isdigit()` (killed only by the U+00B2 alphabet and fixture `². x`) and drop the `.` check (`1- -`); in `_scan_line_matches` change `pos += 1` to `pos = n + 1` after a miss (`\n# #`). Each fails a D5 test. Known equivalent or timing-only mutants, NOT required kills: dropping `!= "\n"` in `_list_item_at`'s leading-space loop gives identical output and is only quadratic on long blank-line runs, which the section 6 timing probe covers; the seven-hash early return was removed from D1 as dead code (`_tail_after_marker` already returns None when the next character is `#`).
5. Architect, after commit and CI: the Sonar search shows the two S8786 issues no longer open on `develop`, with no new issue in `nlp.py`. The scanner form is inferred to clear the rule; if it does not, report, do not suppress.

## 9. Pre-lock gate notes

Claims verified by the architect at `c3916ab`: the two target lines and messages (read in the file and in the Sonar issue list); the differential result (prototype `s8786_proto2.py` and `s8786_unicode.py` in the scratchpad: 52,942,563 inputs over four alphabets, then 9,271,964 over five alphabets with Unicode digits and spaces, 0 disagreements). Consultant review (m_2644, read-only, own harness and seeds): 7,316,111 exhaustive and 60,000 random inputs plus 18 fixtures, 0 disagreements; `\s` equals `str.isspace()`, `\d` equals `str.isdecimal()` and `.` equals not-newline over all 1,114,112 code points on Python 3.11.15; only `nlp.py` 453 and 468 use the patterns (callers `validate_markdown_structure`, one each); estimated helper complexity at most about 12 by hand (complexipy not run). Folded in: three section 8.4 mutants that the corpora could not kill (the `isdigit` premise was wrong because U+0663 is also `isdigit`; the seven-hash check was dead code and is removed; the leading-space mutant is output-equivalent and moved to the timing probe), the `_ordered_marker_end` split, corpus sizes, header-positive fixtures. The architect re-ran the differential on the amended reference code: 16,271,669 inputs over seven alphabets (including U+00B2) and 100,000 random strings, 0 disagreements. Not run: complexipy, Python 3.10, run time under pytest, Sonar's reaction.
