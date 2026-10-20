# SPEC-SONAR-FENCE-SCANNER

## 0. Status

LOCKED 2026-10-20

- Backlog item: none. Sonar rules `python:S6019` (1 issue, `src/saltmdb/utils/nlp.py`) and `python:S8786` (2 issues); at `0ec5fb1` the three call sites are lines 282, 424 and 442 (Sonar's reported lines 282, 425 and 440 come from an older analysis) and the enclosing definitions are `extract_prose_content` (274), `_markdown_headers` (423) and `_markdown_metrics` (433), open on `develop` `0ec5fb1`. Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` at `0ec5fb1` or later; `./verify` exits 0 (2945 passed, 1 skipped, 233 subtests).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the two new helpers directly (differential tests), and `extract_prose_content`, `_markdown_headers`, `_markdown_metrics` through hand-written fixtures with literal expected values.
- Scope (may edit): `src/saltmdb/utils/nlp.py` (the three call sites and two new helpers) and one new test file `tests/test_nlp_fence_scanner.py`.
- Does not touch: any other regex in `nlp.py`, any threshold, the quality-gate logic, any other file.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Three regexes find Markdown code fences: ``` ```[\s\S]*?(?:```|\Z) ``` (line 282, unclosed fence runs to the end of the text), ``` ```[\s\S]*?``` ``` (424, remove closed fences) and the same pattern with `re.findall` (442, collect closed fences). Sonar flags the first as a reluctant quantifier that can match only zero repetitions (S6019) and the other two as super-linear (S8786). The consultant measured the old patterns as near-linear in practice (worst case 30 ms at 80,000 repeats), so this is a Sonar-cleanliness change, not a performance fix. These functions sit on the write path of the memory quality gate (`evaluate_memory_quality`), so equivalence must be proved, not argued. Rejected: possessive or atomic regex forms (need Python 3.11, `pyproject.toml` requires 3.10), splitting line 282 into two alternatives (swaps S6019 for S8786), any suppression or issue-status change (owner rules).

## 2. Decisions

- D1. Add one private helper to `nlp.py`, above `extract_prose_content`:
  `_code_fence_spans(text: str, *, unclosed_to_end: bool = False) -> list[tuple[int, int]]` returning the `(start, end)` index pairs of fenced blocks. Algorithm with `str.find` only: `i = text.find("```")`; while `i != -1`: `j = text.find("```", i + 3)`; if `j == -1`: if `unclosed_to_end` append `(i, len(text))`; stop. Otherwise append `(i, j + 3)` and continue with `i = text.find("```", j + 3)`. The closer search starts at `i + 3`, exactly as the regex's lazy quantifier does, so ```` ```` ```` (four backticks) is not a closed fence and a fence may open at the character after a closer. No regex inside the helper. Add a second private helper next to it, `_replace_code_fences(text: str, replacement: str, *, unclosed_to_end: bool = False) -> str`, that returns `text` with every span of `_code_fence_spans(text, unclosed_to_end=unclosed_to_end)` replaced by `replacement` (build the result from the pieces between spans; one loop, used by D2 and D3, so the rebuild logic exists once).
- D2. `extract_prose_content` (line 282): replace the `re.sub` with `_replace_code_fences(text, " ", unclosed_to_end=True)`; the rest of the function is unchanged.
- D3. `_markdown_headers` (424): `text_no_code` is `_replace_code_fences(text, "")`.
- D4. `_markdown_metrics` (442): `code_blocks` is the list of `text[start:end]` for the spans of `_code_fence_spans(text)`, in order. The other lines of the function are unchanged.
- D5. The new test file `tests/test_nlp_fence_scanner.py` keeps the three OLD patterns as oracles (copied verbatim as module constants with a comment saying they are the pre-change implementation) and compares, at the helper level, three shapes per input: `P282.sub(" ", t) == _replace_code_fences(t, " ", unclosed_to_end=True)`; `PCLOSED.sub("", t) == _replace_code_fences(t, "")`; `PCLOSED.findall(t) == [t[s:e] for s, e in _code_fence_spans(t)]`. Inputs:
  1. Exhaustive: every string over the alphabet backtick, `a`, newline, space, of length 0 to 9 (349,525 inputs; measured about 0.7 s for all three shapes). If a run ever exceeds ten seconds the maximum length drops to 8 and the spec is amended, not the test silently.
  2. A seeded random set (fixed `random.Random(seed)`, at least 20,000 strings of length 10 to 400, backticks about half of the characters; about 1.7 s).
  3. Hand-written fixtures: typed fences, inline code, a closed fence followed by an unclosed one, backtick runs of length 3 to 9, a fence at the very start and the very end, an empty text, text with a single fence.
  Separately, through the real functions on the hand-written fixtures only, with LITERAL expected values that the developer captures by running the UNMODIFIED code at `0ec5fb1` before editing `nlp.py`: the `extract_prose_content` output, and `validate_markdown_structure`'s `has_header_skip`, `untyped_blocks` and `msdi` (no existing test pins the exact output or a fence-bearing `msdi`). Corpus strings are generated or fictional; no memory content in the repository.
- D6. No new Sonar finding: no unused locals, no nested ternaries, no new regex with a lazy quantifier. `ruff check --select ARG,F,RET` on `nlp.py` reports no hit the baseline copy does not.

## 3. Changes per file

| File | Change |
|---|---|
| `src/saltmdb/utils/nlp.py` | new `_code_fence_spans` and `_replace_code_fences`; three call sites (D2-D4) |
| `tests/test_nlp_fence_scanner.py` | new differential tests (D5) |

## 4. Tests

The new file only; no existing test changes. The oracle constants and the literal fixture values are the pre-change behaviour; the file fails on the unmodified source (ImportError) and on each section 8.4 mutant. Expected values come from the old regexes and from the unmodified code, not from the new code.

## 5. Documentation

None.

## 6. Verification procedure

`./verify`; the new test file in isolation; `ruff check --select ARG,F,RET src/saltmdb/utils/nlp.py` against the baseline copy; a timing probe on the new helpers (20,000, 40,000 and 80,000 repeats of ``` ``a ```, of unclosed ``` ```x ``` and of an alternating fence text) that reports linear growth and stays below 100 ms at 80,000.

## 7. Out of scope

The other fence-related regexes (`^\s*```\s*(\S*)` at 434 and `^\s*```` at 458), the inline-code regex, every other Sonar finding.

## 8. Acceptance

1. `./verify` exits 0.
2. `git diff --stat` lists only `src/saltmdb/utils/nlp.py` and the new test file.
3. The differential tests exercise 349,525 exhaustive + at least 20,000 random inputs per shape and pass; they use the old patterns as oracles.
4. Mutation sample on a scratch copy (repo tests as killers, tester names them): change the closer search start from `i + 3` to `i + 1`, to `i + 2`; drop the `unclosed_to_end` branch; continue after `j` instead of `j + 3`; swap `" "` for `""` at line 282; each fails a D5 differential test (the helper-level exhaustive or fixture tests; the mutants die on short inputs such as five or six backticks and "```", but through the full pipeline the final strip and whitespace collapse hide some of them, which is why D5 compares at the helper level).
5. Architect, after commit and CI: the Sonar search shows the three issues (S6019 x1, S8786 x2) no longer open on `develop`, with no new issue in `nlp.py`. The scanner form is inferred to clear the rules; if any remains open, report it, do not suppress it.

## 9. Pre-lock gate notes

Consultant review (m_2624) ran the D1 algorithm against the three old regexes: 349,525 exhaustive inputs and 20,000 seeded random strings, 0 disagreements on all three shapes; all five section 8.4 mutants killed on short inputs; run time 0.7 s exhaustive and 1.7 s random; timing probe linear (alternating fences 37 ms at 320k characters); loop terminates (the next opener index is at least `j + 3`, `j` at least `i + 3`) and is O(n); the patterns are inline literals with no dependents; callers are `_quality_readability_findings` (621, for `extract_prose_content`) and `validate_markdown_structure` (471, 475). Folded in: the blocker about building D5 on the real functions and the duplicated rebuild loop (second helper, helper-level oracle, literal fixtures), the exhaustive count, the line-number reconciliation, the section 4 ImportError wording. Not executed: pytest or CI timing; the Sonar analysis after the change.
