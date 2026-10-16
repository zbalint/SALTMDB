# SPEC-SONAR-PY-BEHAVIOR

## 0. Status

LOCKED 2026-10-16

- Backlog item: none (Sonar cleanup). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` after SPEC-SONAR-PY-STYLE is committed (commit following b0ad081); `./verify` exits 0.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the public functions named in section 3, called directly from the existing test files.
- Scope (may edit), source:
  - `src/saltmdb/domain/services/memory_service/validation.py` (S2583)
  - `src/saltmdb/daemon/client.py` (S2737, and S1172 on `_identify_probe`, `ensure_fresh`, `force_reconnect`)
  - `src/saltmdb/utils/nlp.py` (S8786 at line 446 only)
  - `src/saltmdb/viewer/templates.py` (S1172 on `get_frontend_html`)
  - `src/saltmdb/domain/services/memory_service/lifecycle.py` (S1172 on `_assemble_memory_record`, and its call sites)
  - `src/saltmdb/domain/services/relation_service.py` (S1172 on `resolve_or_create_predicate`, and `_assemble_memory_record` call sites)
- Scope (may edit), tests: `tests/test_daemon_client.py` (fakes and calls for `ensure_fresh`/`force_reconnect`), `tests/test_relation_service.py` (the `resolve_or_create_predicate` call that passes `agent_id`), and the existing nlp test file that covers `utils/nlp.py` (new characterization test for D5; find it with `rg -l "evaluate_memory_quality|list_item" tests`). No other test edits.
- Does not touch: everything else, in particular `write.py`, `cli.py`, `server.py`, `cohesion_service.py`, `duplicates.py`, `ranking.py`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

These Sonar findings are not purely cosmetic: removing a parameter changes a signature and rewriting a regex can change what it matches. A consultant review of the code decided, row by row, which are safe. The owner's rule: never close or hide an issue and never guess; whatever is not listed as a fix here stays open and goes to the owner in the report.

## 2. Decisions

- D1. Behavior is unchanged. No suppression comments (`# noqa`, `NOSONAR`).
- D2. S2583 (`validation.py:29`, `_retrieval_text_hash`): write `hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None`. Inside the `if value` branch the value is always truthy, so `or ""` is dead.
- D3. S2737 (`daemon/client.py:912`, the `except Exception: raise` that ends the method just before `force_reconnect`): remove the try/except and keep the comment above `self.open()`, reworded so it still says that a failed refresh propagates on purpose and why.
- D4. S1172, parameter removal. Fix exactly these six, updating the callers listed (the consultant grepped src/, tests/, hooks/, scripts/ at `5a4e7ac`):
  - `viewer/templates.py:23` `get_frontend_html(db_path)`: no caller passes it.
  - `daemon/client.py:207` `_identify_probe(db_path)`: callers at `client.py:228` and `:450` pass it positionally; drop the argument there. Tests patch it with `return_value` only.
  - `lifecycle.py:746` `_assemble_memory_record(entity_id)`: drop the argument at `lifecycle.py:907`, `:985` and `relation_service.py:851`, `:888`.
  - `client.py:880` `ensure_fresh(db_path)` and `client.py:914` `force_reconnect(db_path)`: drop the argument at `client.py:592`, `:605`, `:661`; update `tests/test_daemon_client.py:194` and `:610` and the fakes at `:343`, `:396`, `:494`, `:549` (written `refresh(_db_path)` / `fake_force_reconnect(_db_path)`) to the new signatures.
  - `relation_service.py:46` `resolve_or_create_predicate(agent_id)`: drop the keyword at `tests/test_relation_service.py:707`.
  After each removal run `rg -n "<name>" src tests hooks scripts` and confirm no caller still passes the removed argument. If a caller you did not expect turns up, stop and report; do not edit outside section 0 scope.
- D5. S8786 at `nlp.py:446` (list-item regex `^\s*(?:[\-\*\+]|\d+\.)\s+(.+)$`, `re.MULTILINE`): change only the leading `^\s*` to `^[^\S\n]*`. Do not use `^[ \t]*`: it would drop `\r`, `\f`, `\v` and `\xa0` prefixes. The consultant found `findall` on `"\n"*20000 + "x"` takes about 5.7 s today, and a 200,000-string fuzz over `" \t\n\r\v\f\xa0-*+1.ab#"` found no mismatch with the new form. Add a characterization test first (literal expected lists) covering: an indented bullet, a `\r`/`\f`/`\xa0`-prefixed bullet, a numbered item, a blank-line-separated list, and a very long run of newlines; assert results, not timing. In the report, repeat a short fuzz comparison of old versus new pattern.
- D6. Not fixed, and reported to the owner with the reason:
  - `nlp.py:279` S6019: Sonar's claim is false. `re.sub` on `"a ```code``` b ```open tail"` returns `"a   b  "`, so the lazy quantifier matches more than zero repetitions. Do not rewrite.
  - `nlp.py:424` S8786 (header regex): no super-linear input was found, and a "simplified" form would change behavior (`"#\nfoo"` currently matches).
  - S1172: `ranking.py:695` (`policy`, documented as kept on purpose, exported), `server.py:460` (`transport_session_id`, carries `# noqa: ARG002`), `cohesion_service.py:47` (`db_path`, about 20 callers and fakes), `duplicates.py:34` (`tags`, caller in out-of-scope `write.py`), `validation.py:178` (`metadata`, never read although a comment says it checks metadata shape: possible lost validation), `nlp.py:509` (`title`, every production caller passes a title: possible lost scoring).
  - All four `S3516` findings in `cli.py`: the always-0 exit code is deliberate (hooks must never fail a session).

## 3. Changes per file

| Rule | File | Line | Fix |
|---|---|---|---|
| `S2583` | `src/saltmdb/domain/services/memory_service/validation.py` | 29 | D2 |
| `S2737` | `src/saltmdb/daemon/client.py` | 912 | D3 |
| `S1172` | `src/saltmdb/daemon/client.py` | 207, 880, 914 | D4 |
| `S1172` | `src/saltmdb/viewer/templates.py` | 23 | D4 |
| `S1172` | `src/saltmdb/domain/services/memory_service/lifecycle.py` | 746 | D4 |
| `S1172` | `src/saltmdb/domain/services/relation_service.py` | 46 | D4 |
| `S8786` | `src/saltmdb/utils/nlp.py` | 446 | D5 |

## 4. Tests

Failing first where behavior could change: the D5 characterization test (it passes before and after the regex change, so no red phase exists for it; that is expected). D4 edits update the existing tests named in D4. The rest rely on the existing suite.

## 5. Documentation

None.

## 6. Verification procedure

`./verify` only. Do not start parallel full runs.

## 7. Out of scope

Everything in D6. All other Sonar rules.

## 8. Acceptance

1. `./verify` exits 0.
2. `git diff --stat` lists only files named in section 0 scope.
3. `rg -n "_identify_probe\(|ensure_fresh\(|force_reconnect\(|_assemble_memory_record\(|get_frontend_html\(|resolve_or_create_predicate\(" src tests hooks scripts` shows no call that passes a removed argument (read each hit).
4. Mutation check for the D5 test: in the assigned checkout, change the regex so it drops `\r`-prefixed bullets (for example `^[ \t]*`), show the new test fails, restore the intended pattern. The tree must match your own diff afterward.
5. The report lists every finding fixed and, for each D6 item, confirms it was left unchanged.
6. Architect, after review and commit: `scripts/prepush-sonar.sh` and a Sonar re-read.

## 9. Pre-lock gate notes

Claims verified by the consultant against `5a4e7ac` (line anchors, callers, regex behavior probes with `python3 -I`); see the assignment event. Scope list reconciled with the caller list in D4 and the grep in acceptance 3. Gate step 7: the six removals are named in D4, section 3 and section 0 identically.
