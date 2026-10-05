# SPEC-<NAME>

Copy this file to `docs/specs/SPEC-<NAME>.md`. Delete these instructions and any section that does
not apply, but keep the numbering of the sections you keep. A spec is committed: never put SALTMDB
memory content (titles, excerpts, quotes, ids) in it.

## 0. Status

State only, no reviewer names. Use `DRAFT` until the pre-lock gate (section 9) passes, then `LOCKED`
with the date.

- Backlog item and shared `context_id`.
- Baseline: commit and the exact test command with its result counts.
- Location and branch (main checkout or worktree).
- Test seams: the public interfaces the tests go through.
- Scope (may edit): an exact file list, with line ranges where it matters.
- Does not touch: files and areas that must stay unchanged.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

The problem, how it was found, and the decision taken. Name the alternatives that were rejected and
why, so nobody re-opens them.

## 2. Decisions

One numbered decision (D1, D2, ...) per choice, each testable. Mark who decided when it matters.

## 3. Changes per file

One subsection per file in scope: the mechanical change, as exact as the reader needs. If a change
deletes a call site, grep the replacement text for every variable that call site used.

## 4. Tests

Written failing first. Expected values are literals or worked examples, never recomputed with the
logic under test.

## 5. Documentation

Which docs change (`docs/architecture.md`, `MIGRATION.md`, tool descriptions) and what they say.

## 6. Verification procedure

Only when the default gate needs more than `./verify`, such as resource limits on a shared host.

## 7. Out of scope

Things a reader might expect and this spec does not do.

## 8. Acceptance

Numbered commands in run order, each with its expected result. Include `git diff --stat` listing
only the files in section 0 scope.

## 9. Pre-lock gate notes

Record the checks run before locking: scope list vs. acceptance list, every data shape described in
more than one place, every claim about existing code backed by a line or test, and a re-grep of the
whole spec after any review-driven change. Later changes go in `## Amendment N (date)` sections
below, each naming the contradiction it resolves.
