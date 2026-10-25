# SPEC-FUZZ-INPUT-EDGES: property tests for five more untrusted-input edges, and the two `json.loads` sinks that still crash on hostile text

## 0. Status

LOCKED.

- Context id: `saltmdb-fuzz-2026-10`. Location: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`, starting from the commit that adds this spec. Builds on `SPEC-FUZZ-FOUNDATION.md` (Hypothesis, profiles `ci` and `fuzz`, `tests/conftest.py`); that spec is committed and not edited here.
- Scope (files that may be edited or created): `src/saltmdb/mcp/tools.py` (the `except` in `_normalize_list_or_str` only), `src/saltmdb/daemon/discovery.py` (the `except` in `read` only), `CONTRIBUTING.md` (the fuzz command and its lead-in sentence in the "Property-based boundary tests" subsection), and NEW `tests/test_fuzz_mcp_args.py`, `tests/test_fuzz_discovery.py`, `tests/test_fuzz_redaction.py`, `tests/test_fuzz_viewer_host.py`.
- Does not touch: `pyproject.toml`, `uv.lock`, `tests/conftest.py`, the redaction patterns, `_host_allowed`, `cursor_offset`, any other `json.loads` site, `docs/BACKLOG.md` (the architect closes BL-087 afterwards), anything in `.github/`.
- Public test seams: `saltmdb.mcp.tools._normalize_list_or_str`, `saltmdb.daemon.discovery.read` and `.discovery_path`, `saltmdb.utils.redaction.redact_secrets`, `saltmdb.viewer.routes.base.ViewerHandlerBase._host_allowed` (built as in `tests/test_viewer_host_validation.py`), `saltmdb.utils.pagination.cursor_offset`.

## 1. Why

`SPEC-FUZZ-FOUNDATION` proved the method on the daemon frame decoder: generated hostile input found an uncaught `RecursionError` and `ValueError` that example tests never reached. BL-087 recorded two more sinks of the same family. This slice (a) fixes them with the same one-line change, and (b) puts five more edges that take text from a less trusted source under property tests: MCP list arguments, the daemon discovery file, secret redaction, the viewer `Host` check and the offset-cursor grammar.

Probes at spec time (repo `.venv`, Python 3.11.15, Hypothesis 6.170.9):
- `_normalize_list_or_str("[" * 100000 + "]" * 100000)` raises `RecursionError`; `_normalize_list_or_str("[" + "9" * 5000 + "]")` raises `ValueError` (the integer conversion limit; it is not a `json.JSONDecodeError`, which is the only exception caught). The function is called on the MCP arguments `tags_filter`, `memory_ids`, `relations` and `tags` (`tools.py` lines 735, 767, 907, 1080), so any MCP caller controls the string.
- `discovery.read(key)` on a file (mode 0o600, owned by the user) holding 100000 nested `[` raises `RecursionError`; a 5000-digit integer returns `None` (caught as `ValueError`). The docstring promises "never as an error".
- `redact_secrets`: no super-linear time on 13 adversarial 200000-character shapes (slowest 0.17 s); a Hypothesis probe (4000 examples, documented token formats embedded in random text) found no leak and the function was idempotent. These become guard tests, not fixes.
- `_host_allowed` agrees with an independent regex oracle over 20000 generated `Host` values (probe), so the property is a contract test on the old code.
- `cursor_offset` rejects Arabic-Indic digits and a 5000-digit run (probe).

Known limits left alone: the `\b` that ends most token patterns means a token whose final character is `-` is matched one character short (or not at all at the minimum length); the redaction pattern set is incomplete for other credential forms (separate backlog work). Neither is asserted by these tests.

## 2. Decisions

- D1. `tools.py` `_normalize_list_or_str`: the handler `except json.JSONDecodeError as exc:` becomes `except (ValueError, RecursionError) as exc:`. The debug log message and the fall-through to the string fallback stay. `ValueError` covers `json.JSONDecodeError` and the integer conversion limit.
- D2. `discovery.py` `read`: `except (OSError, ValueError):` becomes `except (OSError, ValueError, RecursionError):`. Nothing else in the function changes.
- D3. Every property that guards a found defect carries a pinned `@example` (depth 200000; 5000 digits). Strategies mix generated structure with the domain vocabulary below and use no `.filter()`.
- D4. Tests are `unittest` style with Hypothesis `@given`, like `tests/test_fuzz_daemon_frame.py`; no network, no `saltmdb.db`.

## 3. Changes per file

| File | Change |
|---|---|
| `src/saltmdb/mcp/tools.py` | D1 (line 127, the `except` after `json.loads(val_str)`) |
| `src/saltmdb/daemon/discovery.py` | D2 (line 155, the `except` in `read`) |
| `CONTRIBUTING.md` | line 117, the `HYPOTHESIS_PROFILE=fuzz` command lists the two old files; replace them with `tests/test_fuzz_*.py`; and the sentence at line 112 ("Property tests for untrusted daemon frames and FTS query text") becomes "Property tests for untrusted-input boundaries" |
| `tests/test_fuzz_mcp_args.py` | new, section 4 (M1, M2) |
| `tests/test_fuzz_discovery.py` | new (S1) |
| `tests/test_fuzz_redaction.py` | new (R1 to R3) |
| `tests/test_fuzz_viewer_host.py` | new (H1) |

## 4. Tests

Written failing first where the old code fails. A "contract" test passes on the old code and is named as such.

`tests/test_fuzz_mcp_args.py`:
- M1 (abuse; fails on the old code). `_normalize_list_or_str(text)` returns a `list` and never raises, for `text` from `st.one_of` of: `st.text(max_size=40)` mixed with the characters `[ ] , " \ 9 -`; `"[" + "9" * n + "]"` with `n` 4290 to 6000; `"[" * n + "]" * n` with `n` 2000 to 400000; a valid JSON array of ints built with `json.dumps`. Pinned: depth 200000 and a 5000-digit element. Old code: `RecursionError` and `ValueError`.
- M2 (contract). `cursor_offset(cursor, max_offset)` against an independent oracle written in the test: for a `str`, the result is `(int(n), None)` exactly when the string matches `offset:(0|[1-9][0-9]*)` with ASCII digits only and `int(n) <= max_offset`, otherwise `(0, <message>)`; for `None` it is `(0, None)`; for any other object `(0, "cursor must be a string.")`. Strategy: pieces `offset:`, `0`, `1`, `9`, Arabic-Indic digits, `+`, `-`, space, `\n`, long digit runs, joined; `max_offset` from `st.integers(0, 10**6)` plus the pinned 10000. Each `@example` pins BOTH arguments (cursor, `max_offset`): `("offset:1٣", 10000)` (a non-leading Arabic-Indic digit; the leading digit is the literal class `[1-9]`, which is ASCII even without `re.ASCII`, so only a non-leading non-ASCII digit shows the flag), `("offset:٣", 10000)`, `("offset:" + "9" * 5000, 10000)`, `("offset:007", 10000)`, `("offset:10000\n", 10000)`, `("offset:10000", 10000)` and `("offset:0", 0)` (the boundary where `offset > max_offset` differs from `>=`), `("offset:10001", 10000)`, and the non-string cases `(b"offset:1", 10)`, `(1, 10)`, `(None, 10)`. The generated cursors are strings only; the non-string cases are the pins.

`tests/test_fuzz_discovery.py`:
- S1 (abuse; fails on the old code). POSIX only (`unittest.skipIf(sys.platform == "win32")`). `setUp` creates a `tempfile.TemporaryDirectory()` (cleaned by `addCleanup`) and starts `unittest.mock.patch.dict(os.environ, {"HOME": d, "USERPROFILE": d})` (stopped by `addCleanup`); do NOT use pytest's `monkeypatch` or `tmp_path` fixtures inside `@given` (Hypothesis's `function_scoped_fixture` health check fails the test). `setUp` runs once per test method, so each example overwrites the same file: it writes the generated bytes to `discovery.discovery_path(key)`, then `os.chmod(path, 0o600)` (accepted by `_owned_file`, which rejects only another owner or the 0o022 bits), and asserts `discovery.read(key)` returns a `dict` or `None`, never raising. Bodies: the M1 hostile builders as bytes (nesting `n` at most 200000 here, so the `fuzz` profile does not write gigabytes), arbitrary `st.binary(max_size=256)`, and valid records. Pinned: depth 200000. Old code: `RecursionError`.

`tests/test_fuzz_redaction.py`:
- R1 (contract). For `st.text()`: `redact_secrets(redact_secrets(t)) == redact_secrets(t)` and the result is a `str`.
- R2 (contract). Documented token forms embedded TWICE as `prefix + " " + token_a + " " + middle + " " + token_b + " " + suffix` (`prefix`, `middle`, `suffix` from `st.text(max_size=40)`; `token_a` and `token_b` independently drawn from the forms below, so a replacement that stops after the first match leaves the second token): `ghp_` plus 36 to 60 alphanumerics; `AKIA` plus 16 of `[A-Z0-9]`; `sk-ant-` plus 19 to 79 of `[A-Za-z0-9_-]` plus one alphanumeric; `xoxb-` plus 9 to 39 of `[A-Za-z0-9-]` plus one alphanumeric; `sk_live_` plus 20 to 40 alphanumerics; and an assignment `<name><sep><quote><value>` with `name` in `api_key`, `API-KEY`, `password`, `Secret_Key`, `auth-token`, `sep` in `:`, `=`, ` = `, `: `, `quote` in `''`, `'`, `"`, `value` 8 to 40 of `[A-Za-z0-9_./+-]`. The secret (the token, or the value of an assignment, with trailing quote and `-` removed) must occur in the output, after replacing every `[REDACTED_SECRET]` marker in the output by a NUL character (the marker text itself contains valid assignment values such as `REDACTED`), no more often than in `prefix + " " + middle + " " + suffix`. Pinned (token_a, token_b) pairs: `ghp_` plus 36 `A` and `ghp_` plus 36 `B` (the minimum length of the first pattern, N6), and `password=abcdefgh` twice (the minimum value length, N7). Known swallow, not asserted: a prefix that ends in a credential keyword and separator (`pwd:`) followed by an assignment name lets the first match consume the name.
- R3 (contract, coarse time bound). For each of: `"a" * N`, `"a-" * (N // 2)`, `"a." * (N // 2)`, `"a" * 30 + ":" + "b" * N`, `"password=" + "a" * N`, `"sk-" + "a" * N`, `"api_key " * (N // 8)`, with `N = 200000`, one call to `redact_secrets` finishes in under 5 seconds (measured at spec time: 0.17 s at worst; the margin is 30 times). A plain loop, not `@given`.

`tests/test_fuzz_viewer_host.py`:
- H1 (contract). With the handler built as in `HostValidationUnitTests._handler` (server address `("127.0.0.1", 8080)`), for a `Host` value joined from the pieces `localhost`, `LocalHost`, `127.0.0.1`, `[::1]`, `[::1`, `::1`, `8080`, `08080`, `8081`, `٨٠٨٠`, `:`, space, tab, `\r`, `\n`, `\x0b`, `\x00`, `@`, `evil.example`, `localhost.`, `[`, `]`, `%`: `_host_allowed()` is true exactly when `re.fullmatch(r"(localhost|127\.0\.0\.1|\[::1\]):8080", value.strip(" \t"), re.IGNORECASE | re.ASCII)` matches, and it never raises. A second case: zero `Host` headers and two `Host` headers return false. Every true-branch mutant needs a pin because generation mostly produces negatives. Pinned: `localhost:8080`, `LocalHost:8080` (kills the removed `.lower()`), `127.0.0.1:8080`, `[::1]:8080`, `localhost:08080`, `localhost:٨٠٨٠`, ` localhost:8080\t`, `localhost:8080\n` (kills `strip()` for `strip(" \t")`).

The `ci` profile keeps the four new files under 20 seconds together on the development host (record `pytest --durations=5`); if not, lower `max_examples` for the slow property with a per-test `@settings` and say which. Required: `HYPOTHESIS_PROFILE=fuzz` run of all `tests/test_fuzz_*.py` exits 0 (`timeout 1500`); a failure there is a finding, not a reason to weaken a property.

## 5. Out of scope

- New redaction patterns, the trailing-`-` boundary, and any change to `redact_secrets`, `_host_allowed`, `cursor_offset` (BL-087's other sinks, if any exist, get their own probe first).
- Atheris or coverage-guided fuzzing, a scheduled deep-fuzz workflow, mutation automation, MCP tool dispatch fuzzing, hook stdin parsing, tag normalisation (separate specs).
- `docs/BACKLOG.md` (the architect closes BL-087 after acceptance).

## 6. Verification procedure

Every pytest invocation runs under the shared lock: `flock -w 3600 /home/zbalint/.cache/saltmdb-suite.lock systemd-run --user --scope -p MemoryMax=1500M <command>`, with the absolute interpreter `/home/zbalint/workspace/SALTMDB/.venv/bin/python`. The full gate is `./verify` run the same way. One suite run at a time. No dependency change: no `uv lock`.

## 7. Acceptance

1. Old-code red: after writing the tests and before D1 and D2, `<lock> .venv/bin/python -m pytest -q tests/test_fuzz_mcp_args.py tests/test_fuzz_discovery.py tests/test_fuzz_redaction.py tests/test_fuzz_viewer_host.py --durations=5` fails exactly M1 and S1 (the contract tests M2, R1 to R3 and H1 pass on the old code; name them as such in the report). Report the failing examples.
2. Generation-only proof: with the pinned `@example` lines removed in a scratch copy and `phases=[Phase.generate, Phase.shrink]`, M1 and S1 still find the failure by generation (report the minimal inputs). The checkout is not edited for this.
3. After D1 and D2 the same command passes; report counts and `--durations=5`.
4. `<lock> ./verify` exits 0.
5. Deep run: `HYPOTHESIS_PROFILE=fuzz timeout 1500 <lock> .venv/bin/python -m pytest -q tests/test_fuzz_*.py` exits 0.
6. Mutation check on a scratch copy of the final tree (`git archive` into the scratchpad, absolute interpreter, PYTHONPATH at the copy), each restored: N1 `RecursionError` removed from D1's tuple (M1); N2 D1 reverted to `json.JSONDecodeError` (M1); N3 `RecursionError` removed from D2 (S1); N4 `cursor_offset` regex `re.ASCII` flag removed (M2, via the pin `("offset:1٣", 10000)`); N5 `cursor_offset` `offset > max_offset` changed to `>=` (M2, via the pins `("offset:10000", 10000)` and `("offset:0", 0)`); N6 the first `redact_secrets` pattern `{36,}` changed to `{40,}` (R2); N7 the generic assignment pattern's `{8,}` changed to `{20,}` (R2); N8 `redact_secrets` substitutes with `count=1` (R2, via the two embedded tokens); N9 `_host_allowed` `port_text == str(address[1])` changed to `int(port_text) == address[1]` (H1, via the pins `localhost:08080` and `localhost:٨٠٨٠`); N10 `_host_allowed` host `.lower()` removed (H1, via `LocalHost`); N11 `_host_allowed` `strip(" \t")` changed to `strip()` (H1, via `\n`). Name the failing test of each; a surviving mutant is reported with its reason.
7. `rg -n 'import hypothesis|from hypothesis' src` shows nothing; `git status --short` lists only the section 0 scope files; `git diff --check` is clean.

## 8. Pre-lock gate notes

| Claim | Where | Verified how |
|---|---|---|
| `_normalize_list_or_str` catches only `json.JSONDecodeError`; deep nesting raises `RecursionError`, 5000 digits raises `ValueError` | `tools.py` 114 to 134 | read; probed with the repo `.venv` |
| it is called on MCP arguments | `tools.py` 735, 767, 907, 1080 | read (`rg -n _normalize_list_or_str src tests`) |
| the only existing test of it passes a malformed array and expects the string fallback | `tests/test_security_quality_gates.py` 65 | read |
| `discovery.read` catches `(OSError, ValueError)`; a nested file raises `RecursionError`, a 5000-digit file returns `None` | `discovery.py` 144 to 157 | read; probed with `HOME` at a temp dir |
| `discovery_path` is under `~/.saltmdb` and `HOME` redirects it | `discovery.py` 79 to 86 | read; probed; `tests/test_discovery_validation.py` uses the same redirect |
| `redact_secrets` is one compiled alternation with `re.IGNORECASE`; idempotent and complete for the R2 forms | `redaction.py` 7 to 70 | read; probed with Hypothesis (4000 examples) |
| `redact_secrets` time on seven hostile shapes at N = 200000 | probe | measured, slowest 0.17 s |
| `_host_allowed` equals the regex oracle | `base.py` 44 to 66 | probed over 20000 generated values |
| `cursor_offset` pattern and bounds | `utils/pagination.py` | read; probed |
| existing tests that pin the changed `except` clauses | `tests/test_search_memory_characterization.py` 1093 raises `JSONDecodeError` in an unrelated search path; no test names `RecursionError` | `rg -n -e JSONDecodeError -e RecursionError tests hooks/tests` (this output, read) |

## Review record (2026-10-25)

Accepted after developer implementation (the developer later replaced the cursor oracle by an independent unbounded ASCII grammar, then reverified) and an independent tester pass: old-code red is exactly M1 and S1, generation alone finds both defects (nesting depth 2000, 4301 digits), N1 to N11 killed, seven tester mutants of the new tests killed (each deleted token pattern, dropped `IGNORECASE`, an injected catastrophic pattern caught by R3), three deterministic `ci` runs. Accepted survivors: an empty `[REDACTED_SECRET]` marker survives the new redaction test (R2 checks that tokens disappear, not the marker text; the marker is pinned by `tests/test_redaction.py` and other existing tests), and a malformed-bracket mutant of `_host_allowed` that changes nothing observable (equivalent). Architect `./verify` on the final tree: 3763 passed, 10 skipped, 481 subtests.
