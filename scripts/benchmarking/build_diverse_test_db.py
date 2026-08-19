"""Ingest a sample of `test_data/` (12 HuggingFace datasets, gitignored) into a safe copy of the
live SALTMDB corpus, for downstream re-derivation of `src/saltmdb/config.py`'s similarity-
threshold constants (`DEDUP_*`, `RERANK_*`, `COHESION_*`, `CLUSTER_*`, `SUPERSESSION_*`,
`RELATION_GATE_*`) against realistically diverse, multilingual content instead of hand-crafted
English coding-domain example pairs. See SALTMDB memory `cd084ced` for the original gap this
closes, and the plan this script implements (`~/.claude/plans/cheeky-plotting-tulip.md`, Rev 6,
Codex-approved) for the full design rationale.

Calls `saltmdb.domain.services.memory_service.store_memory()` in-process (direct import, not the
MCP layer) against a hardened copy of the live DB, so the resulting corpus goes through the real
dedup/supersession/quality/embedding pipeline exactly as a live agent write would.

Deliberately NOT run against the live default DB (`get_db_path()`/`~/.saltmdb/saltmdb.db`) -- see
`resolve_and_guard_destination` below. Point `--db-path` at a throwaway destination; `--source-db-
path` (default `get_db_path()`) is only ever read from (`sqlite3.Connection.backup()`), never
written to.

Its pure logic (selection ranking, frontmatter parsing, checkpoint I/O, path guarding, outcome
classification, split-group-id derivation) lives in importable top-level functions so it can be
unit-tested directly against synthetic fixtures -- see `tests/test_build_diverse_test_db.py`.
"""

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SELECTION_ALGORITHM_VERSION = "stable-rank-sha256-v1"

# All 12 datasets currently in test_data/ (Context section of the plan): 6 English
# (one-row-per-example, no shared source-article concept) + 6 multilingual Wikipedia
# (one-row-per-article-section, title-bearing, shares the split/leakage-grouping concern with
# squad). Tag slugs are `dataset.replace("_", "-")` applied at call sites, not stored here, so
# this list stays the single source of truth for `--datasets`' default and validation.
ALL_DATASETS = [
    "ag_news",
    "cnn_dailymail",
    "imdb",
    "squad",
    "wikitext",
    "yelp_review",
    "wikipedia_french",
    "wikipedia_russian",
    "wikipedia_spanish",
    "wikipedia_arabic",
    "wikipedia_chinese",
    "wikipedia_hungarian",
]

# Datasets whose frontmatter carries a `title` field (Step 4/"New-dataset frontmatter" of the
# plan) -- squad plus all six wikipedia_* sets. Drives both the frontmatter allowlist's `title`
# expectation and split_group_id's title-vs-original_document_id branch, but neither of those
# actually branches on this set membership directly -- both are driven by whether `title` was
# actually parsed out of a given file (defensive: a title-bearing dataset with one malformed row
# should not silently promote that row to the title-driven grouping path with a null title).
TITLE_BEARING_DATASETS = {
    "squad",
    "wikipedia_french",
    "wikipedia_russian",
    "wikipedia_spanish",
    "wikipedia_arabic",
    "wikipedia_chinese",
    "wikipedia_hungarian",
}

FILENAME_RE = re.compile(r"^doc_(\d+)_part_(\d+)\.md$")
FRONTMATTER_KEY_RE = re.compile(r"^(source_dataset|label|title|url):\s*(.*)$")


# --------------------------------------------------------------------------------------------
# Step 1 -- safe, hardened destination handling
# --------------------------------------------------------------------------------------------


class DestinationGuardError(Exception):
    """Base class for every resolve_and_guard_destination() rejection."""


class RefuseLiveDBError(DestinationGuardError):
    pass


class SamePathError(DestinationGuardError):
    pass


class ResumeTargetMissingError(DestinationGuardError):
    pass


class DestinationExistsError(DestinationGuardError):
    pass


def resolve_and_guard_destination(
    source_db_path: str, dest_db_path: str, overwrite: bool, resume: bool, live_db_path: str
) -> tuple[Path, Path]:
    """Resolve source/dest to absolute, symlink-free paths and enforce the safety invariants.

    `Path.resolve()` follows symlinks, so a symlink-to-live or symlink-to-source destination is
    caught the same way as a direct path match. Checkpoint/manifest paths must always be derived
    from the RESOLVED destination this function returns, never the raw --db-path string, so a
    relative path can't produce mismatched sidecar files.
    """
    src = Path(source_db_path).resolve()
    dst = Path(dest_db_path).resolve()
    live = Path(live_db_path).resolve()

    if dst == live or (dst.exists() and dst.samefile(live)):
        raise RefuseLiveDBError(f"destination resolves to the live DB path: {dst}")
    if dst == src:
        raise SamePathError("destination must not equal the source DB path")

    if resume:
        if not dst.exists():
            raise ResumeTargetMissingError(f"--resume given but {dst} does not exist")
    elif dst.exists() and not overwrite:
        raise DestinationExistsError(f"{dst} already exists; pass --overwrite or --resume")

    return src, dst


def _cleanup_wal_shm_sidecars(dst: Path) -> None:
    """Delete dst and its -wal/-shm siblings together, before a fresh .backup() copy.

    Only called on the --overwrite path, after resolve_and_guard_destination has already
    cleared the guards. Never called on --resume -- those sidecars may be the live, in-progress
    WAL/SHM pair for the destination DB being resumed; deleting them would corrupt or lose
    uncheckpointed committed data from the run being resumed.
    """
    if dst.exists():
        dst.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = dst.with_name(dst.name + suffix)
        if sidecar.exists():
            sidecar.unlink()


def prepare_destination_db(source_db_path: str, dest_db_path: str, overwrite: bool) -> None:
    """Step 1's fresh copy: sqlite3 .backup() API, correct under WAL mode (mirrors
    src/saltmdb/db/backup.py:create_snapshot's technique). Caller must have already run
    resolve_and_guard_destination -- this performs no guarding of its own.
    """
    dst = Path(dest_db_path)
    if overwrite:
        _cleanup_wal_shm_sidecars(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    src_conn = sqlite3.connect(source_db_path)
    dest_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()


# --------------------------------------------------------------------------------------------
# Step 2 -- deterministic, resume-safe sampling
# --------------------------------------------------------------------------------------------


def candidate_rank(seed: str, source_relpath: str) -> str:
    return hashlib.sha256(f"{seed}\0{source_relpath}".encode()).hexdigest()


def select_top_n(candidates: list[str], seed: str, n: int | None) -> list[str]:
    """Stable rank over the full candidate list -- selected(seed, 20) is always a strict prefix
    of selected(seed, 40) by construction, so growing --n-per-dataset on a resumed run never
    reprocesses or drops an already-selected record. n=None (--n-per-dataset all) is the same
    function with no truncation, not a separate code path.
    """
    ranked = sorted(candidates, key=lambda p: candidate_rank(seed, p))
    return ranked if n is None else ranked[:n]


def enumerate_candidates(dataset_dir: Path) -> list[str]:
    """Deterministic sorted directory walk, relative to dataset_dir's parent (test_data/) --
    the returned strings ARE source_relpath, this run's checkpoint record_key.
    """
    root = dataset_dir.parent
    return sorted(str(p.relative_to(root)) for p in dataset_dir.rglob("*.md"))


# --------------------------------------------------------------------------------------------
# Step 3 -- document identity + safe frontmatter parsing
# --------------------------------------------------------------------------------------------


@dataclass
class ParsedDoc:
    dataset: str
    source_relpath: str
    source_document_id: str
    part_index: int
    body: str
    source_dataset_field: str | None
    hf_label: int | None
    source_title: str | None
    source_url: str | None


@dataclass
class ParseOutcome:
    outcome: str  # "parsed" | "malformed_file" | "unsupported_filename"
    doc: ParsedDoc | None = None
    error_detail: str | None = None


def _strip_scalar_value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    return raw


def parse_frontmatter_file(path: Path, dataset: str, source_relpath: str) -> ParseOutcome:
    """Line-based, allowlist-only parser -- NEVER a YAML load (squad's `answers` field is a
    `!!python/object/apply:numpy...` pickle tag that a real YAML loader would choke on or, worse,
    execute). See Step 3 of the plan for the full spec this implements.
    """
    m = FILENAME_RE.match(path.name)
    if not m:
        return ParseOutcome(outcome="unsupported_filename", error_detail=f"filename: {path.name}")
    # source_document_id is the bare filename digits (e.g. "012345"); original_document_id
    # (dataset-qualified, computed in build_metadata) is the collision-safe identifier -- see
    # Step 3 of the plan. Kept separate so source_document_id stays a clean display/title value.
    source_document_id = m.group(1)
    part_index = int(m.group(2))

    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        return ParseOutcome(outcome="malformed_file", error_detail=f"utf-8 decode error: {e}")

    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return ParseOutcome(outcome="malformed_file", error_detail="missing opening '---'")

    close_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close_idx = i
            break
    if close_idx is None:
        return ParseOutcome(outcome="malformed_file", error_detail="missing closing '---'")

    frontmatter_lines = lines[1:close_idx]
    body = "\n".join(lines[close_idx + 1 :]).strip("\n")

    fields: dict[str, str] = {}
    for line in frontmatter_lines:
        fm = FRONTMATTER_KEY_RE.match(line)
        if fm:
            fields[fm.group(1)] = _strip_scalar_value(fm.group(2))

    source_dataset_field = fields.get("source_dataset")
    if not source_dataset_field:
        return ParseOutcome(outcome="malformed_file", error_detail="missing source_dataset field")

    hf_label: int | None = None
    if "label" in fields:
        try:
            hf_label = int(fields["label"])
        except ValueError:
            hf_label = None

    source_title = fields.get("title") or None
    source_url = fields.get("url") or None

    doc = ParsedDoc(
        dataset=dataset,
        source_relpath=source_relpath,
        source_document_id=source_document_id,
        part_index=part_index,
        body=body,
        source_dataset_field=source_dataset_field,
        hf_label=hf_label,
        source_title=source_title,
        source_url=source_url,
    )
    return ParseOutcome(outcome="parsed", doc=doc)


# --------------------------------------------------------------------------------------------
# Split/leakage grouping key -- see plan section "Split/leakage grouping key"
# --------------------------------------------------------------------------------------------


def compute_split_group_id(
    dataset: str, source_title: str | None, original_document_id: str
) -> str:
    """f"{dataset}:{source_title}" when a title exists (squad + all six wikipedia_* sets --
    dataset-qualified so identical titles across languages/datasets never collide), else
    original_document_id (the five non-title datasets, inherently one-row-per-example). Any
    future train/test split or before/after evaluation MUST group by this, never source_title or
    original_document_id directly, and never split individual rows independently.
    """
    if source_title:
        return f"{dataset}:{source_title}"
    return original_document_id


# --------------------------------------------------------------------------------------------
# Step 4 -- ingest via the real pipeline
# --------------------------------------------------------------------------------------------

TAG_VOCABULARY = ["benchmark-corpus"] + [d.replace("_", "-") for d in ALL_DATASETS]


def build_title(dataset: str, source_document_id: str, part_index: int) -> str:
    base = f"[{dataset}] {source_document_id}"
    if part_index > 0:
        base += f" part {part_index}"
    return base


def build_metadata(doc: ParsedDoc, run_id: str) -> dict:
    original_document_id = f"{doc.dataset}:{doc.source_document_id}"
    split_group_id = compute_split_group_id(doc.dataset, doc.source_title, original_document_id)
    return {
        "source_dataset": doc.dataset,
        "source_document_id": doc.source_document_id,
        "original_document_id": original_document_id,
        "part_index": doc.part_index,
        "source_relpath": doc.source_relpath,
        "hf_label": doc.hf_label,
        "source_title": doc.source_title,
        "source_url": doc.source_url,
        "split_group_id": split_group_id,
        "ingestion_run_id": run_id,
    }


# --------------------------------------------------------------------------------------------
# Step 7 -- outcome classification
# --------------------------------------------------------------------------------------------


def _classify_store_envelope(result: dict) -> tuple[str, str | None]:
    status = result.get("status")
    if status == "ok":
        data = result.get("data")
        if isinstance(data, dict) and isinstance(data.get("id"), str):
            return "stored_clean", None
        return "other_error", str(result)
    if status != "rejected":
        return "other_error", str(result)

    errors = result.get("errors") or []
    codes = {
        item.get("code")
        for item in errors
        if isinstance(item, dict) and isinstance(item.get("code"), str)
    }
    if "REJECT_EXACT_DUPLICATE" in codes:
        outcome = "exact_duplicate_rejected"
    elif (
        any(
            isinstance(item, dict)
            and "quality check rejected" in str(item.get("message", "")).lower()
            for item in errors
        )
        or "MEMORY_QUALITY_REJECTED" in codes
    ):
        outcome = "quality_rejected"
    else:
        outcome = "other_error"
    return outcome, str(result)


def _classify_legacy_store_result(result: str) -> tuple[str, str | None]:
    if result.startswith("Knowledge stored successfully"):
        return "stored_clean", None
    if "REJECT_EXACT_DUPLICATE" in result:
        return "exact_duplicate_rejected", result
    if "Memory quality check rejected" in result:
        return "quality_rejected", result
    return "other_error", result


def classify_store_result(result: str | dict) -> tuple[str, str | None]:
    """Classify store_memory()'s return into a checkpoint outcome. Returns (outcome, error_detail).

    Phase 5 store_memory responses use the uniform response envelope. Successful writes carry the
    entity ID in ``data.id``; deterministic failures carry structured ``errors`` entries. Plain
    strings remain accepted as a defensive fallback for older validation failures, but all normal
    success and rejection paths are classified from the envelope.
    """
    if isinstance(result, dict):
        return _classify_store_envelope(result)
    return _classify_legacy_store_result(result)


# --------------------------------------------------------------------------------------------
# Step 5 -- checkpoint + resume
# --------------------------------------------------------------------------------------------


@dataclass
class CheckpointState:
    records: list[dict] = field(default_factory=list)
    processed_keys: set = field(default_factory=set)

    def add(self, record: dict) -> None:
        self.records.append(record)
        self.processed_keys.add(record["record_key"])


def load_checkpoint(checkpoint_path: Path) -> CheckpointState:
    if not checkpoint_path.exists():
        return CheckpointState()
    data = json.loads(checkpoint_path.read_text())
    state = CheckpointState()
    for rec in data.get("records", []):
        state.add(rec)
    return state


def write_checkpoint(checkpoint_path: Path, state: CheckpointState) -> None:
    checkpoint_path.write_text(json.dumps({"records": state.records}, indent=2))


# --------------------------------------------------------------------------------------------
# Step 6 -- completion barrier
# --------------------------------------------------------------------------------------------


def check_embedding_completion(conn: sqlite3.Connection, owner_id: str) -> dict:
    """Both checks must be fully clean for corpus_embedding_complete=True:
      - entities.embedding_status counts for owner_id -- any pending/NULL/failed row fails.
      - entity_chunk_embeddings freshness (content_hash-matched, not a status column) -- any
        non-archived entity missing a fresh chunk row fails.
    Never reports ready when it isn't.

    entity_chunk_embeddings is a vec0 virtual table -- querying it (even a plain SELECT/JOIN,
    no vector math) requires the sqlite_vec extension loaded on THIS specific connection object,
    same as every other reader of that table in the codebase (semantic_search,
    _batch_semantic_similarities, rerank_candidates_by_topic). Loading twice on an
    already-loaded connection is a harmless no-op (confirmed in vector_schema.py).
    """
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    status_rows = conn.execute(
        "SELECT embedding_status, COUNT(*) FROM entities "
        "WHERE owner_id = ? AND status != 'archived' GROUP BY embedding_status",
        (owner_id,),
    ).fetchall()
    status_counts: dict[str, int] = {}
    for status, count in status_rows:
        status_counts[status or "NULL"] = count
    entity_level_ready = set(status_counts.keys()) <= {"ready"} and status_counts

    total_active = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE owner_id = ? AND status != 'archived'", (owner_id,)
    ).fetchone()[0]
    fresh_chunk_entities = conn.execute(
        """
        SELECT COUNT(DISTINCT e.id) FROM entities e
        JOIN entity_chunk_embeddings c ON c.entity_id = e.id AND c.content_hash IS e.content_hash
        WHERE e.owner_id = ? AND e.status != 'archived'
        """,
        (owner_id,),
    ).fetchone()[0]
    chunk_stale_count = total_active - fresh_chunk_entities
    chunk_level_ready = total_active > 0 and chunk_stale_count == 0

    return {
        "embedding_status_counts": status_counts,
        "entity_level_ready": bool(entity_level_ready),
        "chunk_fresh_entities": fresh_chunk_entities,
        "chunk_stale_entities": chunk_stale_count,
        "chunk_level_ready": chunk_level_ready,
        "corpus_embedding_complete": bool(entity_level_ready) and chunk_level_ready,
    }


# --------------------------------------------------------------------------------------------
# Main ingestion loop
# --------------------------------------------------------------------------------------------

OWNER_ID = "benchmark_corpus"


def run_ingestion(  # noqa: C901, PLR0912, PLR0915
    dest_db_path: str,
    datasets: list[str],
    n_per_dataset: int | None,
    seed: str,
    test_data_dir: Path,
    checkpoint_every: int,
    run_id: str,
) -> dict:
    from saltmdb.db.connection import get_connection, close_connection
    from saltmdb.domain.services import memory_service

    checkpoint_path = Path(f"{dest_db_path}.checkpoint.json")
    state = load_checkpoint(checkpoint_path)

    conn = get_connection(dest_db_path)
    per_dataset_stats: dict[str, dict] = {d: _new_dataset_stats() for d in datasets}
    t_start = time.monotonic()
    total_attempted = 0
    since_last_flush = 0

    try:
        for dataset in datasets:
            dataset_dir = test_data_dir / dataset
            if not dataset_dir.exists():
                logger.warning("Dataset dir missing, skipping: %s", dataset_dir)
                continue

            candidates = enumerate_candidates(dataset_dir)
            selected = select_top_n(candidates, seed, n_per_dataset)
            stats = per_dataset_stats[dataset]
            stats["selected"] = len(selected)

            already = [k for k in selected if k in state.processed_keys]
            todo = [k for k in selected if k not in state.processed_keys]
            stats["resumed_skipped"] = len(already)

            for record_key in todo:
                path = test_data_dir / record_key
                outcome_record = {
                    "record_key": record_key,
                    "dataset": dataset,
                    "ingestion_run_id": run_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                parse_result = parse_frontmatter_file(path, dataset, record_key)
                if parse_result.outcome != "parsed":
                    outcome_record["outcome"] = parse_result.outcome
                    outcome_record["error_detail"] = parse_result.error_detail
                    stats[parse_result.outcome] = stats.get(parse_result.outcome, 0) + 1
                else:
                    doc = parse_result.doc
                    metadata = build_metadata(doc, run_id)
                    title = build_title(dataset, doc.source_document_id, doc.part_index)
                    tags = ["benchmark-corpus", dataset.replace("_", "-")]
                    result = memory_service.store_memory(
                        content=doc.body,
                        owner_id=OWNER_ID,
                        scope="shared",
                        title=title,
                        tags=tags,
                        metadata=metadata,
                        context_id=None,
                        db_connection=conn,
                        # db_path is NOT redundant with db_connection here: store_memory only
                        # derives its own db_path from get_connection() when db_connection is
                        # falsy. Passing db_connection alone leaves store_memory's internal
                        # db_path variable at its default (None) for the rest of the call, so
                        # the async embed-pool jobs and trigger_librarian() -- which both read
                        # `db_path or get_db_path()` -- would silently target the LIVE default
                        # DB instead of this destination copy. Must pass both explicitly.
                        db_path=dest_db_path,
                    )
                    outcome, error_detail = classify_store_result(result)
                    outcome_record["outcome"] = outcome
                    outcome_record["error_detail"] = error_detail
                    if outcome == "stored_clean":
                        entity_id = _extract_entity_id(result)
                        outcome_record["entity_id"] = entity_id
                        stats["split_group_ids"].add(metadata["split_group_id"])
                    stats[outcome] = stats.get(outcome, 0) + 1

                state.add(outcome_record)
                total_attempted += 1
                since_last_flush += 1
                if since_last_flush >= checkpoint_every:
                    write_checkpoint(checkpoint_path, state)
                    since_last_flush = 0
                    elapsed = time.monotonic() - t_start
                    rate = total_attempted / elapsed if elapsed > 0 else 0.0
                    logger.info(
                        "progress: %d attempted this run, %.1f docs/sec, dataset=%s",
                        total_attempted,
                        rate,
                        dataset,
                    )

        write_checkpoint(checkpoint_path, state)

        # Step 6: completion barrier -- drain this process's embed-pool jobs, then verify.
        memory_service._embed_pool.shutdown(wait=True)
        # Recreate the pool so a later store_memory call in the same process (e.g. a test
        # harness reusing this module) isn't left with a permanently-shutdown executor.
        from concurrent.futures import ThreadPoolExecutor

        memory_service._embed_pool = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="saltmdb-embed"
        )

        embedding_check = check_embedding_completion(conn, OWNER_ID)

        run_scoped_count = conn.execute(
            "SELECT COUNT(*) FROM events e WHERE e.type = 'supersession_candidate' "
            "AND json_extract(e.content, '$.run_id') = ?",
            (run_id,),
        ).fetchone()[0]
        cumulative_count = conn.execute(
            """
            SELECT COUNT(*) FROM events e
            JOIN entities en ON json_extract(e.content, '$.new_entity_id') = en.id
            WHERE e.type = 'supersession_candidate' AND en.owner_id = ?
            """,
            (OWNER_ID,),
        ).fetchone()[0]

        archived_count = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE owner_id = ? AND status = 'archived'", (OWNER_ID,)
        ).fetchone()[0]
        total_entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]

    finally:
        close_connection(conn)

    manifest = {
        "run_id": run_id,
        "seed": seed,
        "selection_algorithm_version": SELECTION_ALGORITHM_VERSION,
        "wall_clock_seconds": round(time.monotonic() - t_start, 2),
        "attempted_this_invocation": total_attempted,
        "attempted_cumulative": len(state.records),
        "datasets": {
            d: _finalize_dataset_stats(per_dataset_stats[d])
            for d in datasets
            if d in per_dataset_stats
        },
        "supersession_candidate_count_this_run": run_scoped_count,
        "supersession_candidate_count_cumulative": cumulative_count,
        "archived_entity_count": archived_count,
        "resulting_db_total_entity_count": total_entity_count,
        "embedding_completion": embedding_check,
        "corpus_embedding_complete": embedding_check["corpus_embedding_complete"],
    }
    return manifest


def _new_dataset_stats() -> dict:
    return {"selected": 0, "resumed_skipped": 0, "split_group_ids": set()}


def _finalize_dataset_stats(stats: dict) -> dict:
    out = dict(stats)
    out["distinct_split_group_ids"] = len(out.pop("split_group_ids"))
    return out


def _extract_entity_id(result: str | dict) -> str | None:
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, dict) and isinstance(data.get("id"), str):
            return data["id"]
        return None
    m = re.search(r"ID:\s*([0-9a-fA-F-]{36})", result)
    return m.group(1) if m else None


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--n-per-dataset",
        default="2000",
        help='Docs to sample per dataset: an integer, or "all" for unbounded (default 2000).',
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        default=ALL_DATASETS,
        choices=ALL_DATASETS,
        help="Datasets to ingest (default: all twelve).",
    )
    parser.add_argument(
        "--db-path", required=True, help="Destination DB path (never the live path)."
    )
    parser.add_argument("--seed", default="diverse-corpus-v1", help="Selection seed.")
    parser.add_argument(
        "--source-db-path", default=None, help="DB to copy from (default: live get_db_path())."
    )
    parser.add_argument(
        "--test-data-dir",
        default=None,
        help="Path to test_data/ (default: repo_root/test_data).",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--checkpoint-every", type=int, default=250)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from saltmdb.config import get_db_path

    source_db_path = args.source_db_path or get_db_path()
    live_db_path = get_db_path()

    try:
        src, dst = resolve_and_guard_destination(
            source_db_path, args.db_path, args.overwrite, args.resume, live_db_path
        )
    except DestinationGuardError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.n_per_dataset.strip().lower() == "all":
        n_per_dataset = None
    else:
        n_per_dataset = int(args.n_per_dataset)

    test_data_dir = (
        Path(args.test_data_dir)
        if args.test_data_dir
        else Path(__file__).resolve().parents[2] / "test_data"
    )

    if not args.resume:
        print(f"Preparing destination DB copy: {src} -> {dst}")
        prepare_destination_db(str(src), str(dst), args.overwrite)
    else:
        print(f"Resuming against existing destination DB: {dst}")

    run_id = str(uuid.uuid4())
    print(
        f"run_id={run_id}  seed={args.seed}  n_per_dataset={args.n_per_dataset}  datasets={args.datasets}"
    )

    manifest = run_ingestion(
        dest_db_path=str(dst),
        datasets=args.datasets,
        n_per_dataset=n_per_dataset,
        seed=args.seed,
        test_data_dir=test_data_dir,
        checkpoint_every=args.checkpoint_every,
        run_id=run_id,
    )

    manifest_path = Path(f"{dst}.manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest written to {manifest_path}")
    print(json.dumps(manifest, indent=2))

    if not manifest["corpus_embedding_complete"]:
        print(
            "\nWARNING: corpus_embedding_complete is FALSE -- some entities are not yet "
            "embedded/chunk-fresh. Re-run with --resume (no new records to attempt, but this "
            "does not re-check embeddings on its own) or inspect embedding_completion in the "
            "manifest.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
