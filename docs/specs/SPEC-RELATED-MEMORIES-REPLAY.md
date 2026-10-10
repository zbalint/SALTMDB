# SPEC-RELATED-MEMORIES-REPLAY

## 0. Status

LOCKED 2026-10-10.

- Backlog item: BL-028. Context id: `answer-side-memory-hook-2026-10-10`.
- Baseline: the commit that contains the accepted BL-027 work (`ecf3ea7` plus the BL-027 commit), `./verify` exit 0.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, `develop`.
- Test seams: the pure functions of `scripts/benchmarking/replay_related_memories.py`
  (`select_replies`, `rewind_copy`, `build_packets`, `analyze_labels`) and its `main(argv)`.
- Scope (may edit): `scripts/benchmarking/replay_related_memories.py` (new),
  `tests/test_replay_related_memories.py` (new).
- Does not touch: `src/`, `hooks/`, other scripts, `docs/` (no memory content may be written
  there), `.gitignore`, `docs/BACKLOG.md`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

The answer-side memory hook needs a relevance threshold. The shipped default (4.0) was tuned for
near-duplicate pairs, and the known positive example scores far below the search relevance gate.
The threshold must be measured on real assistant replies, on the memories that existed when each
reply was written. A replay on the live database would be contaminated: memories stored later
the same day quote the replies. This slice builds an offline harness that (1) takes a snapshot
copy (BL-027), (2) rewinds a working copy to each reply's time, (3) scores every candidate with
the production service, (4) writes labelling packets for a judge, and (5) turns labels into
precision, false-block and block-rate numbers per threshold.

Rejected: replaying on the live daemon (single-owner rule, contamination); per-reply
`exclude_ids` for later memories (the semantic request is capped at 50, so a long exclude list
starves the pool); archiving alone (`search_primitives.semantic_search` applies the scope and
status filters after the vec0 nearest-neighbour step, so rewound memories would still occupy the
`k` nearest slots and starve the candidate pool); deleting entity rows (FTS and other tables make
that error-prone); labels produced by the harness itself (circular).

## 2. Decisions

- D1. One script, `argparse`, `main(argv) -> int`, subcommands `prepare` and `analyze`. The header
  does the `sys.path` insert of its own directory and `src/` like
  `replay_historical_search_accuracy.py`; no `scripts/__init__.py`.
- D2. `prepare --snapshot S --out-dir O [--sample N=200] [--seed 1] [--min-chars 200]
  [--limit-candidates 8] [--include-traces FILE]`:
  1. Guard, exit 2 on any failure (catch `RuntimeError`): the resolved `S` and `O/work.db` must
     both pass `run_evaluation_matrix._refuse_unsafe_db_path` (imported lazily inside the guard
     function, because that module's top-level imports are heavy; tests patch `get_db_path`);
     additionally `S` must not be under the repository tree, `O` must be outside the repository
     or under a `scratch*/` directory, and `O/work.db` must not already exist. Nothing is written
     when the guard refuses.
  2. Copy `S` to `O/work.db` with `shutil.copy2`. Never modify `S`.
  3. `select_replies(conn, n, seed, min_chars, include_ids)`: traces with `status='completed'`,
     `final_assistant_message` length >= `min_chars`, one per distinct
     `final_assistant_message_hash`. Traces listed in `--include-traces` (JSONL lines
     `{"trace_id": ...}`) are always selected first; the remaining slots are filled from the
     rest sorted by id (lexicographic) and sampled with `random.Random(seed)`. The result is
     ordered newest first by reply time.
  4. Reply time is `completed_at` when set, otherwise `created_at`. All time comparisons
     normalise both sides with SQLite `datetime(x)` (entity and trace timestamps come in both the
     space-separated and the ISO `T` forms).
  5. For each reply, newest first, `rewind_copy(conn, cutoff)` makes the copy look as it did at
     the cutoff, and only ever moves backwards (it is never restored):
     - every entity with `datetime(created_at) >= datetime(cutoff)` gets `status='archived'`
       (hides it from the FTS channel and, through the service clause, from the pool);
     - for each such entity, `embedding_service.clear_embedding_vectors_for_entity(conn, id,
       strict=True)` removes its vectors (the vec0 nearest-neighbour step runs before the status
       filter, so archived rows would otherwise still take neighbour slots);
     - every relation whose source or target entity was rewound, or whose own
       `datetime(created_at) >= datetime(cutoff)`, is deleted;
     - on the remaining relations, a `valid_to` or `invalid_at` at or after the cutoff is set to
       NULL (the edge was still valid at reply time; the bitemporal check compares with now).
  6. Score the reply with `related_memory_service.find_related_memories(conn, O/work.db, text,
     agent_id=<trace agent_id>, with_all=True)` in-process, timed with `time.perf_counter`.
     The in-process latency excludes the RPC and is not the hook's latency.
  7. Write `O/candidates.jsonl`: per reply `trace_id`, `reply_time`, `agent_id`, `latency_ms`,
     `segments`, and `candidates`: the best `--limit-candidates` service rows ordered by score
     descending then id ascending (`id`, `score`, `segment`, `fts_rank`, `semantic_distance`).
     Write `O/packets/NNN.jsonl` (50 replies each): the last 1500 chars of the reply and, per
     candidate in `candidates`, `id`, `title` and the first 600 chars of the content, read from
     `work.db`. These files contain memory content and live only under `O`.
  8. Write `O/MANIFEST.json`: harness name, git commit, snapshot file name and size, sample size,
     seed, counts (replies, with candidates, without), min and max reply time, and the known
     limitations: memories archived or edited after a reply are missing or changed in the copy.
  9. Write `O/JUDGE_INSTRUCTIONS.txt` (D3 text).
- D3. Labels (written by the owner or a judge agent, never the harness): JSONL, one line per
  `(trace_id, memory_id)` with `label` in `relevant`, `partly`, `irrelevant`. Judge text: a
  candidate is `relevant` when a reader of this reply would want to see that memory before moving
  on, because it states a decision, constraint or earlier finding the reply repeats, contradicts
  or builds on.
- D4. `analyze --dir O --labels L [--known-positives K] [--thresholds 2,3,4,5,6,7,8] [--limit 3]`
  (K: JSONL lines `{"trace_id": ..., "memory_id": ...}`). Every candidate in `candidates.jsonl`
  must be labelled; otherwise exit 2 and print the unlabelled count. Per threshold `t`, the shown
  set of a reply is its `candidates` with `score >= t`, in the stored order, cut to `limit`.
  Reported per threshold, each with its counts next to it (replies, blocked replies, shown
  candidates, and shown `relevant` / `partly` / `irrelevant`):
  - `block_rate` = replies with a non-empty shown set / all replies;
  - `precision` = (relevant + 0.5 * partly) / shown candidates, `null` when nothing is shown;
  - `false_block_rate` = replies with a non-empty shown set and no `relevant` candidate / all
    replies;
  - `known_hits` / `known_total`.
  With zero replies every rate is `null` and the command exits 2. Latency: nearest-rank p50 and
  p95 over all replies except the first (warm-up), with the first reported separately.
  Output: JSON on stdout and `O/analysis.json`.
- D5. The ship bar is printed next to the numbers, not enforced: precision >= 0.7,
  false_block_rate <= 0.15, block_rate <= 0.20, p95 latency < 3000 ms, `known_hits ==
  known_total`. A `null` precision never passes. `analyze` names the lowest threshold meeting
  all of it, or `none`.
- D6. No network, no writes outside `O`, no SALTMDB tool calls, no daemon. The bundled models may
  load; tests patch them.

## 3. Changes per file

- `replay_related_memories.py`: `select_replies`, `rewind_copy`, `build_packets`,
  `analyze_labels`, the two commands, JSONL helpers. No new dependency.
- `tests/test_replay_related_memories.py`: loads the script with
  `importlib.util.spec_from_file_location` like `tests/test_eval_stats.py`.

## 4. Tests

(Small temp databases built with the project's schema helpers; embeddings are small fixed
vectors with `sqlite_vec` loaded; the cross-encoder is patched.)

- T1. `select_replies`: drops incomplete, short and duplicate-hash traces; includes the forced
  traces; the same seed gives the same sample; newest first by reply time; `completed_at` wins
  over `created_at`.
- T2. `rewind_copy`: a later entity is archived and loses its vector row; an earlier one is
  untouched; a relation from a later entity is deleted; a relation created after the cutoff
  between two earlier entities is deleted, so the earlier target is not superseded; an edge with
  `valid_to` after the cutoff and before now is kept with `valid_to` NULL; both timestamp forms
  (`2026-10-10 12:00:00` and `2026-10-10T12:00:00+00:00`) order correctly against a cutoff in
  either form; a second, earlier cutoff only rewinds more.
- T3. Semantic pool: for a query whose nearest neighbours were all rewound, the service still
  returns its older candidates (the pool is not shorter), and no rewound entity appears in the
  FTS or semantic channel.
- T4. Guard: the live path, the repository path, an unsafe output directory and an existing
  `work.db` exit 2 and write nothing; a successful run leaves the snapshot byte-identical.
- T5. `prepare` end to end with a patched service: files exist, manifest counts are right,
  packets cap content at 600 chars and the reply tail at 1500, `candidates` is cut to
  `--limit-candidates` in score-then-id order.
- T6. `analyze_labels` with literal fixtures: a worked example of 4 replies and 3 thresholds with
  literal `block_rate`, `precision`, `false_block_rate` and `known_hits`, including a reply with
  an empty shown set (not a false block) and a tie at the limit boundary (id order decides).
- T7. `analyze` exits 2 and names the count when a candidate is unlabelled, and when there are
  zero replies; the lowest passing threshold is `none` when nothing meets the bar; a `null`
  precision does not pass.
- T8. Latency: a literal list gives literal nearest-rank p50 and p95, excluding the first value.

## 5. Out of scope

Running the replay on the real snapshot, producing labels, choosing the final threshold, the
stop hook, changes to the service, a CI job, committing any output.

## 6. Acceptance

1. `.venv/bin/python -m pytest tests/test_replay_related_memories.py -q` green.
2. `rg -n "sqlite3\.connect" scripts/benchmarking/replay_related_memories.py` matches only code
   that opens `O/work.db` (reviewed by hand).
3. `git status --short` lists only the two files in section 0.
4. `./verify` exit 0, one full run.

## 7. Pre-lock gate notes

Run before locking, 2026-10-10:

- Probe (in-memory vec0 table, fixed 2-d vectors): deleting the five nearest rows makes the next
  k=5 neighbour query return the next five, so deleting vectors frees neighbour slots. The
  semantic step applies the status and scope filters after the neighbour step
  (`search_primitives.semantic_search`), which is why archiving alone is not enough.
- Checked in source: `relations.valid_at` and `invalid_at` exist through the schema migration;
  `embedding_service.clear_embedding_vectors_for_entity(conn, id, strict=True)` deletes from
  `entity_embeddings` and `entity_chunk_embeddings` and is reused; archiving fires the FTS delete
  triggers; `_refuse_unsafe_db_path` raises `RuntimeError` and only checks the live path and the
  shared fixture, so the repository-tree and output-directory checks are local; `scratch*/` is
  ignored; tests load scripts with `importlib.util.spec_from_file_location`.
- The consultant review changed the design (relations by own `created_at`, expired-later edges,
  helper reuse, reply time, timestamp normalisation, guard details, metric definitions,
  forced traces); the changes were applied and D1-D6 re-read against each other. No second pass.
- Not checked before lock: whether a real snapshot has an active embedding generation table
  other than `entity_embeddings` (the service reads the literal name); the developer reports it
  if the snapshot differs. Acceptance 1 runs after implementation.
