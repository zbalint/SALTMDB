"""Offline replay harness for the answer-side related-memory hook (SPEC-RELATED-MEMORIES-REPLAY).

``prepare`` copies a snapshot made by ``saltmdb-cli snapshot`` to ``<out>/work.db``, rewinds that
copy to each sampled reply's time (newest reply first, so the copy only ever moves backwards),
scores the reply with the production ``related_memory_service`` in-process, and writes
candidates, labelling packets, a manifest and judge instructions under ``<out>``. ``analyze``
turns judge labels into block rate, precision and false-block rate per threshold.

Everything under ``<out>`` contains memory content: keep it outside the repository or under a
gitignored ``scratch*/`` directory, and never commit it. The latency measured here is the
in-process service time; it excludes the RPC and is not the hook's latency.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from evaluation_artifacts import git_commit_fingerprint  # noqa: E402
from saltmdb.domain.services import embedding_service, related_memory_service  # noqa: E402

PACKET_REPLIES = 50
REPLY_TAIL_CHARS = 1500
CONTENT_HEAD_CHARS = 600
LABELS = ("relevant", "partly", "irrelevant")
SHIP_BAR = {
    "precision_min": 0.7,
    "false_block_rate_max": 0.15,
    "block_rate_max": 0.20,
    "p95_latency_ms_below": 3000,
    "known_hits_equal_known_total": True,
}
LIMITATIONS = [
    "Memories archived after a reply are missing from the copy, and memories edited after a "
    "reply appear with their later content.",
    "Latency is the in-process service time on the copy; it excludes the RPC and is not the "
    "hook's latency.",
    "Memories the agent wrote during the reply's own turn but before completed_at stay visible "
    "in the replay, so they can inflate the relevant counts.",
]
JUDGE_INSTRUCTIONS = """\
Label every candidate in packets/*.jsonl. Write one JSONL line per (trace_id, memory_id):
{"trace_id": "...", "memory_id": "...", "label": "relevant" | "partly" | "irrelevant"}

A candidate is "relevant" when a reader of this reply would want to see that memory before
moving on, because it states a decision, constraint or earlier finding the reply repeats,
contradicts or builds on. Use "partly" when it touches the same subject but would only
marginally help that reader, and "irrelevant" otherwise.

Judge from the reply tail and the memory text shown; do not look anything up. These files hold
memory content: keep them local and never commit them.
"""


def _guard(snapshot: str, out_dir: str) -> None:
    """Raise RuntimeError unless the snapshot and output paths are safe. Writes nothing."""
    from run_evaluation_matrix import _refuse_unsafe_db_path  # heavy module: import lazily

    snap = Path(os.path.realpath(snapshot))
    out = Path(os.path.realpath(out_dir))
    work = out / "work.db"
    _refuse_unsafe_db_path(str(snap))
    _refuse_unsafe_db_path(str(work))
    repo = _REPO_ROOT.resolve()
    if snap.is_relative_to(repo):
        raise RuntimeError(f"Refusing a snapshot inside the repository: {snap}")
    inside = out.relative_to(repo).parts if out.is_relative_to(repo) else None
    if inside is not None and not (inside and inside[0].startswith("scratch")):
        raise RuntimeError(f"Output must be outside the repository or under scratch*/: {out}")
    if not snap.is_file():
        raise RuntimeError(f"No snapshot file at {snap}")
    if work.exists():
        raise RuntimeError(f"Refusing to overwrite an existing work copy: {work}")


def select_replies(
    conn: sqlite3.Connection, n: int, seed: int, min_chars: int, include_ids: list[str]
) -> list[dict[str, Any]]:
    """Sample completed replies, one per distinct reply hash, forced traces first; newest first."""
    rows = conn.execute(
        """SELECT id, agent_id, final_assistant_message, final_assistant_message_hash,
                  COALESCE(completed_at, created_at) AS reply_time,
                  datetime(COALESCE(completed_at, created_at)) AS reply_key
           FROM conversation_traces
           WHERE status = 'completed' AND final_assistant_message IS NOT NULL
             AND length(final_assistant_message) >= ?
           ORDER BY id""",
        (min_chars,),
    ).fetchall()
    by_id = {row[0]: row for row in rows}
    forced = [by_id[trace_id] for trace_id in dict.fromkeys(include_ids) if trace_id in by_id]
    seen_hashes = {row[3] for row in forced}
    rest = []
    for row in rows:
        if row[3] in seen_hashes:
            continue
        seen_hashes.add(row[3])
        rest.append(row)
    slots = max(0, n - len(forced))
    sampled = random.Random(seed).sample(rest, min(slots, len(rest)))
    chosen = sorted(forced + sampled, key=lambda row: (row[5] or "", row[0]), reverse=True)
    return [
        {"trace_id": row[0], "agent_id": row[1], "text": row[2], "reply_time": row[4]}
        for row in chosen
    ]


def rewind_copy(conn: sqlite3.Connection, cutoff: str) -> None:
    """Make the work copy look as it did at ``cutoff``. Only ever moves backwards; commits.

    Vectors are removed as well as archiving the row, because the vec0 nearest-neighbour step
    runs before the status filter and archived rows would otherwise keep their neighbour slots.
    Relations created after the cutoff are deleted before expired edges are reopened, so a
    reopened edge cannot collide with a later re-creation under the partial UNIQUE index.
    """
    # shortcut: every call re-clears vectors of all later entities (already-cleared ones are
    # no-op deletes), so cost grows with replies x later entities; track a high-water cutoff if
    # a real replay is too slow.
    later = "SELECT id FROM entities WHERE datetime(created_at) >= datetime(?)"
    if not conn.in_transaction:
        conn.execute("BEGIN")
    try:
        later_ids = [row[0] for row in conn.execute(later, (cutoff,))]
        conn.execute(
            "UPDATE entities SET status = 'archived' "
            "WHERE status != 'archived' AND datetime(created_at) >= datetime(?)",
            (cutoff,),
        )
        for entity_id in later_ids:
            embedding_service.clear_embedding_vectors_for_entity(conn, entity_id, strict=True)
        conn.execute(
            f"""DELETE FROM relations
                WHERE datetime(created_at) >= datetime(?)
                   OR source_id IN ({later}) OR target_id IN ({later})""",
            (cutoff, cutoff, cutoff),
        )
        for column in ("valid_to", "invalid_at"):
            conn.execute(
                f"UPDATE relations SET {column} = NULL "
                f"WHERE {column} IS NOT NULL AND datetime({column}) >= datetime(?)",
                (cutoff,),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def build_packets(
    conn: sqlite3.Connection, scored: list[dict[str, Any]]
) -> list[list[dict[str, Any]]]:
    """Group replies that have candidates into packets of PACKET_REPLIES for the judge."""
    entries = []
    for reply in scored:
        if not reply["candidates"]:
            continue
        candidates = []
        for candidate in reply["candidates"]:
            row = conn.execute(
                "SELECT title, full_content FROM entities WHERE id = ?", (candidate["id"],)
            ).fetchone()
            title, content = row if row else ("", "")
            candidates.append(
                {"id": candidate["id"], "title": title, "content": content[:CONTENT_HEAD_CHARS]}
            )
        entries.append(
            {
                "trace_id": reply["trace_id"],
                "reply_tail": reply["text"][-REPLY_TAIL_CHARS:],
                "candidates": candidates,
            }
        )
    return [entries[i : i + PACKET_REPLIES] for i in range(0, len(entries), PACKET_REPLIES)]


def nearest_rank(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(pct / 100 * len(ordered))) - 1]


def latency_summary(latencies: list[float]) -> dict[str, Any]:
    """Nearest-rank p50/p95 over every reply but the first (warm-up), reported separately."""
    warm = latencies[1:]
    return {
        "first_ms": latencies[0] if latencies else None,
        "count": len(warm),
        "p50_ms": nearest_rank(warm, 50),
        "p95_ms": nearest_rank(warm, 95),
    }


def analyze_labels(
    rows: list[dict[str, Any]],
    labels: dict[tuple[str, str], str],
    known: set[tuple[str, str]],
    thresholds: list[float],
    limit: int,
) -> dict[str, Any]:
    """Per-threshold block rate, precision, false-block rate and known-positive hits."""
    unlabelled = sum(
        1
        for row in rows
        for candidate in row["candidates"]
        if (row["trace_id"], candidate["id"]) not in labels
    )
    if unlabelled:
        raise ValueError(f"{unlabelled} unlabelled candidates")
    replies = len(rows)
    results = []
    for threshold in sorted(thresholds):
        counts = {"replies": replies, "blocked": 0, "shown": 0}
        counts.update({label: 0 for label in LABELS})
        false_blocks = 0
        known_hits = 0
        for row in rows:
            shown = [c for c in row["candidates"] if c["score"] >= threshold][:limit]
            if not shown:
                continue
            counts["blocked"] += 1
            counts["shown"] += len(shown)
            shown_labels = [labels[(row["trace_id"], c["id"])] for c in shown]
            for label in shown_labels:
                counts[label] += 1
            if "relevant" not in shown_labels:
                false_blocks += 1
            known_hits += sum(1 for c in shown if (row["trace_id"], c["id"]) in known)
        results.append(
            {
                "threshold": threshold,
                "block_rate": counts["blocked"] / replies if replies else None,
                "precision": (
                    (counts["relevant"] + 0.5 * counts["partly"]) / counts["shown"]
                    if counts["shown"]
                    else None
                ),
                "false_block_rate": false_blocks / replies if replies else None,
                "known_hits": known_hits,
                "known_total": len(known),
                "counts": counts,
            }
        )
    return {"replies": replies, "thresholds": results}


def _meets_bar(entry: dict[str, Any], p95: float | None) -> bool:
    return (
        entry["precision"] is not None
        and entry["precision"] >= SHIP_BAR["precision_min"]
        and entry["false_block_rate"] is not None
        and entry["false_block_rate"] <= SHIP_BAR["false_block_rate_max"]
        and entry["block_rate"] is not None
        and entry["block_rate"] <= SHIP_BAR["block_rate_max"]
        and p95 is not None
        and p95 < SHIP_BAR["p95_latency_ms_below"]
        and entry["known_hits"] == entry["known_total"]
    )


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_jsonl(path: str, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def cmd_prepare(args: argparse.Namespace) -> int:
    try:
        _guard(args.snapshot, args.out_dir)
    except RuntimeError as exc:
        print(f"# Refused: {exc}", file=sys.stderr)
        return 2
    include_ids = (
        [str(row["trace_id"]) for row in _read_jsonl(args.include_traces)]
        if args.include_traces
        else []
    )
    out = os.path.realpath(args.out_dir)
    work_db = os.path.join(out, "work.db")
    os.makedirs(out, exist_ok=True)
    shutil.copy2(args.snapshot, work_db)

    conn = sqlite3.connect(work_db, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")  # the service opens its own second connection
        replies = select_replies(conn, args.sample, args.seed, args.min_chars, include_ids)
        selected_ids = {reply["trace_id"] for reply in replies}
        forced_dropped = [trace_id for trace_id in include_ids if trace_id not in selected_ids]
        if forced_dropped:
            print(
                f"# Warning: {len(forced_dropped)} --include-traces id(s) not selected "
                f"(missing, incomplete, shorter than --min-chars, or a duplicate reply): "
                f"{', '.join(forced_dropped)}",
                file=sys.stderr,
            )
        scored = []
        for reply in replies:
            rewind_copy(conn, reply["reply_time"])
            started = time.perf_counter()
            rows = related_memory_service.find_related_memories(
                conn, work_db, reply["text"], agent_id=reply["agent_id"], with_all=True
            )
            latency_ms = (time.perf_counter() - started) * 1000
            ordered = sorted(rows, key=lambda row: (-row["score"], row["id"]))
            scored.append(
                {
                    **reply,
                    "latency_ms": latency_ms,
                    "segments": len(related_memory_service.split_segments(reply["text"])),
                    "candidates": [
                        {
                            key: row.get(key)
                            for key in ("id", "score", "segment", "fts_rank", "semantic_distance")
                        }
                        for row in ordered[: args.limit_candidates]
                    ],
                }
            )
        packets = build_packets(conn, scored)
    finally:
        conn.close()

    _write_jsonl(
        os.path.join(out, "candidates.jsonl"),
        [
            {
                key: reply[key]
                for key in (
                    "trace_id",
                    "reply_time",
                    "agent_id",
                    "latency_ms",
                    "segments",
                    "candidates",
                )
            }
            for reply in scored
        ],
    )
    os.makedirs(os.path.join(out, "packets"), exist_ok=True)
    for index, packet in enumerate(packets, start=1):
        _write_jsonl(os.path.join(out, "packets", f"{index:03d}.jsonl"), packet)

    with_candidates = sum(1 for reply in scored if reply["candidates"])
    times = [reply["reply_time"] for reply in reversed(replies)]  # replies are newest first
    manifest = {
        "harness": "replay_related_memories",
        "git_commit": git_commit_fingerprint(_REPO_ROOT),
        "snapshot": {
            "name": os.path.basename(args.snapshot),
            "bytes": os.path.getsize(args.snapshot),
        },
        "sample": args.sample,
        "seed": args.seed,
        "min_chars": args.min_chars,
        "limit_candidates": args.limit_candidates,
        "counts": {
            "replies": len(scored),
            "with_candidates": with_candidates,
            "without": len(scored) - with_candidates,
        },
        "reply_time_min": times[0] if times else None,
        "reply_time_max": times[-1] if times else None,
        "forced_dropped": forced_dropped,
        "limitations": LIMITATIONS,
    }
    with open(os.path.join(out, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    with open(os.path.join(out, "JUDGE_INSTRUCTIONS.txt"), "w", encoding="utf-8") as f:
        f.write(JUDGE_INSTRUCTIONS)
    print(json.dumps(manifest["counts"]))
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    rows = _read_jsonl(os.path.join(args.dir, "candidates.jsonl"))
    labels: dict[tuple[str, str], str] = {}
    unknown: list[str] = []
    conflicts: list[tuple[str, str]] = []
    for row in _read_jsonl(args.labels):
        key = (str(row["trace_id"]), str(row["memory_id"]))
        label = row.get("label")
        if label not in LABELS:
            unknown.append(repr(label))
            continue
        if labels.get(key, label) != label:
            conflicts.append(key)
        labels[key] = label
    if unknown:
        print(
            f"# Error: {len(unknown)} label(s) not in {', '.join(LABELS)}, e.g. {unknown[0]}",
            file=sys.stderr,
        )
        return 2
    if conflicts:
        print(
            f"# Error: {len(conflicts)} (trace_id, memory_id) pair(s) with conflicting labels, "
            f"e.g. {conflicts[0]}",
            file=sys.stderr,
        )
        return 2
    known = (
        {(str(row["trace_id"]), str(row["memory_id"])) for row in _read_jsonl(args.known_positives)}
        if args.known_positives
        else set()
    )
    thresholds = [float(value) for value in args.thresholds.split(",") if value.strip()]
    try:
        result = analyze_labels(rows, labels, known, thresholds, args.limit)
    except ValueError as exc:
        print(f"# Error: {exc}", file=sys.stderr)
        return 2
    latency = latency_summary([float(row["latency_ms"]) for row in rows])
    for entry in result["thresholds"]:
        entry["meets_bar"] = _meets_bar(entry, latency["p95_ms"])
    passing = [entry["threshold"] for entry in result["thresholds"] if entry["meets_bar"]]
    sampled_traces = {str(row["trace_id"]) for row in rows}
    report = {
        **result,
        "known_unsampled": sorted([list(pair) for pair in known if pair[0] not in sampled_traces]),
        "limit": args.limit,
        "latency": latency,
        "ship_bar": SHIP_BAR,
        "lowest_passing_threshold": passing[0] if passing else "none",
    }
    with open(os.path.join(args.dir, "analysis.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    if not rows:
        print("# Error: no replies in candidates.jsonl", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser(
        "prepare", help="Rewind a snapshot copy, score replies, write packets."
    )
    prepare.add_argument("--snapshot", required=True)
    prepare.add_argument("--out-dir", required=True)
    prepare.add_argument("--sample", type=int, default=200)
    prepare.add_argument("--seed", type=int, default=1)
    prepare.add_argument("--min-chars", type=int, default=200)
    prepare.add_argument("--limit-candidates", type=int, default=8)
    prepare.add_argument("--include-traces", default=None, help='JSONL of {"trace_id": ...}.')
    prepare.set_defaults(func=cmd_prepare)

    analyze = sub.add_parser("analyze", help="Turn judge labels into per-threshold metrics.")
    analyze.add_argument("--dir", required=True)
    analyze.add_argument("--labels", required=True)
    analyze.add_argument("--known-positives", default=None)
    analyze.add_argument("--thresholds", default="2,3,4,5,6,7,8")
    analyze.add_argument("--limit", type=int, default=3)
    analyze.set_defaults(func=cmd_analyze)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
