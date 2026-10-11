import os
import queue
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest import mock
from typing import TypeVar

import pytest

from saltmdb import config
from saltmdb.daemon import discovery
from saltmdb.daemon.server import _DaemonState
from saltmdb.db.schema import init_db
from saltmdb.domain.services import embedding_service


_T = TypeVar("_T")
_SEED_ROWS = (
    ("a", "First title", "First body", "hash-a", None, None),
    ("b", "Second title", "Second body", "hash-b", "Retrieval body", "retrieval-b"),
    ("c", "Third title", "Third body", "hash-c", None, None),
)


def _seed_database(path: Path) -> sqlite3.Connection:
    conn = init_db(str(path))
    for entity_id, title, content, content_hash, retrieval_text, retrieval_hash in _SEED_ROWS:
        conn.execute(
            "INSERT INTO entities (id,created_at,updated_at,last_accessed_at,status,title,"
            "full_content,content_hash,retrieval_text,retrieval_text_hash) "
            "VALUES (?, '2020', '2020', '2020', 'raw', ?, ?, ?, ?, ?)",
            (entity_id, title, content, content_hash, retrieval_text, retrieval_hash),
        )
        embedding_service.enqueue_embedding_jobs_for_entity(
            conn, entity_id, title, content, content_hash, force=True
        )
        if retrieval_text is not None:
            embedding_service.enqueue_retrieval_embedding_job_for_entity(
                conn, entity_id, retrieval_text, retrieval_hash, force=True
            )
    conn.commit()
    return conn


@pytest.fixture
def seeded_connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = _seed_database(tmp_path / "recovery.db")
    try:
        yield conn
    finally:
        conn.close()


class _ImmediateCoordinator:
    def __init__(self, conn: sqlite3.Connection, stop: threading.Event | None = None):
        self.conn = conn
        self.stop = stop
        self.submissions = 0

    def submit(self, label: str, fn: Callable[[sqlite3.Connection], _T], *, priority: str) -> _T:
        self.submissions += 1
        result = fn(self.conn)
        if self.stop is not None:
            self.stop.set()
        return result


def test_clean_database_has_no_job_gaps(seeded_connection: sqlite3.Connection):
    assert embedding_service.embedding_recovery_gap_count(seeded_connection) == 0


def test_missing_embedding_jobs_count_distinct_entities(seeded_connection: sqlite3.Connection):
    seeded_connection.execute("DELETE FROM embedding_jobs WHERE entity_id='a'")
    assert embedding_service.embedding_recovery_gap_count(seeded_connection) == 1


def test_missing_retrieval_job_counts_entity(seeded_connection: sqlite3.Connection):
    seeded_connection.execute("DELETE FROM retrieval_embedding_jobs WHERE entity_id='b'")
    assert embedding_service.embedding_recovery_gap_count(seeded_connection) == 1


def test_archived_and_absent_source_hashes_have_no_job_gaps(seeded_connection: sqlite3.Connection):
    seeded_connection.executemany(
        "INSERT INTO entities (id,created_at,updated_at,last_accessed_at,status,title,"
        "full_content,content_hash,retrieval_text_hash) "
        "VALUES (?, '2020', '2020', '2020', ?, 'Excluded title', 'Excluded body', ?, ?)",
        (
            ("archived", "archived", "archived-hash", "archived-retrieval-hash"),
            ("null-hashes", "raw", None, None),
            ("empty-hashes", "raw", "", ""),
        ),
    )
    assert embedding_service.embedding_recovery_gap_count(seeded_connection) == 0


def test_canary_uses_covering_entities_index_and_indexed_job_lookups(
    seeded_connection: sqlite3.Connection,
):
    indexes = {row[1] for row in seeded_connection.execute("PRAGMA index_list(entities)")}
    assert "idx_entities_embedding_recovery" in indexes
    details = [
        row[-1]
        for row in seeded_connection.execute(
            f"EXPLAIN QUERY PLAN {embedding_service.EMBEDDING_RECOVERY_GAP_SQL}"
        )
    ]
    scans = [detail for detail in details if detail.startswith("SCAN e")]
    assert scans, details
    assert all(
        detail.endswith("USING COVERING INDEX idx_entities_embedding_recovery") for detail in scans
    ), details
    assert any(
        detail.startswith("SEARCH j") and "embedding_jobs" in detail for detail in details
    ), details
    assert any(
        detail.startswith("SEARCH r") and "retrieval_embedding_jobs" in detail for detail in details
    ), details
    assert not any(detail.startswith(("SCAN j", "SCAN r")) for detail in details), details


def test_full_reconciliation_pages_and_stops_before_next_page(tmp_path: Path):
    conn = init_db(str(tmp_path / "pages.db"))
    try:
        conn.executemany(
            "INSERT INTO entities (id,created_at,updated_at,last_accessed_at,status,title,"
            "full_content,content_hash) VALUES (?, '2020', '2020', '2020', 'raw', ?, ?, ?)",
            (
                ("a", "First title", "First body", "hash-a"),
                ("b", "Second title", "Second body", "hash-b"),
                ("c", "Third title", "Third body", "hash-c"),
                ("d", "Fourth title", "Fourth body", "hash-d"),
                ("e", "Fifth title", "Fifth body", "hash-e"),
            ),
        )
        assert embedding_service.reconcile_all_embedding_jobs(
            _ImmediateCoordinator(conn), page_size=2
        ) == (5, 0)
        stop = threading.Event()
        coordinator = _ImmediateCoordinator(conn, stop)
        assert embedding_service.reconcile_all_embedding_jobs(
            coordinator, stop=stop, page_size=2
        ) == (2, 0)
        assert coordinator.submissions == 1
    finally:
        conn.close()


def _run_daemon_until_recovery(db_path: Path, marker: str) -> str:
    env = dict(os.environ)
    env["SALTMDB_DB_PATH"] = str(db_path)
    env["SALTMDB_VIEWER_ENABLED"] = "false"
    proc = subprocess.Popen(
        [sys.executable, "-m", "saltmdb.daemon.server", "--foreground"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    stdout = proc.stdout
    lines: list[str] = []
    received: queue.Queue[str | None] = queue.Queue()

    def collect_output():
        for line in stdout:
            lines.append(line)
            received.put(line)
        received.put(None)

    reader = threading.Thread(target=collect_output, daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 30
        recovery_done = False
        signals_ready = False
        # Recovery can finish before main() installs signal handlers. Wait for both within
        # the same startup bound so SIGTERM exercises orderly shutdown, not its default action.
        while not (recovery_done and signals_ready):
            remaining = deadline - time.monotonic()
            assert remaining > 0, (
                f"daemon did not reach recovery/shutdown readiness:\n{''.join(lines)}"
            )
            try:
                line = received.get(timeout=remaining)
            except queue.Empty:
                pytest.fail(f"daemon did not reach recovery/shutdown readiness:\n{''.join(lines)}")
            assert line is not None, f"daemon exited before recovery completed:\n{''.join(lines)}"
            recovery_done = recovery_done or marker in line
            signals_ready = signals_ready or "Signal handlers registered:" in line
        proc.send_signal(signal.SIGTERM)
        try:
            exit_code = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail(f"daemon did not exit within 15s of SIGTERM:\n{''.join(lines)}")
        reader.join(timeout=5)
        output = "".join(lines)
        assert exit_code == 0, output
        return output
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        reader.join(timeout=5)
        stdout.close()
        canonical = discovery.resolve_canonical_db_path(str(db_path))
        try:
            os.remove(discovery.discovery_path(discovery.daemon_key(canonical)))
        except OSError:
            pass


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX SIGTERM delivery")
def test_daemon_ready_precedes_gapped_database_recovery(tmp_path: Path):
    db_path = tmp_path / "gapped.db"
    conn = _seed_database(db_path)
    conn.execute("DELETE FROM embedding_jobs")
    conn.commit()
    conn.close()
    output = _run_daemon_until_recovery(db_path, "Embedding recovery finished")
    assert output.index("SALTMDB daemon ready") < output.index(
        "Reconciled durable embedding jobs for 3 active entities"
    )
    assert "Embedding recovery: 3 entities with job gaps; running full scan" in output
    assert "still have job gaps" not in output


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX SIGTERM delivery")
def test_daemon_skips_full_recovery_on_clean_database(tmp_path: Path):
    db_path = tmp_path / "clean.db"
    _seed_database(db_path).close()
    output = _run_daemon_until_recovery(
        db_path, "Embedding recovery: no job gaps found; full scan skipped"
    )
    assert "Reconciled durable embedding jobs" not in output
    assert "Embedding recovery finished" not in output


def test_full_reconciliation_closes_detected_job_gap(seeded_connection: sqlite3.Connection):
    seeded_connection.execute("DELETE FROM embedding_jobs WHERE entity_id='a'")
    assert embedding_service.embedding_recovery_gap_count(seeded_connection) == 1
    embedding_service.reconcile_all_embedding_jobs(_ImmediateCoordinator(seeded_connection))
    assert embedding_service.embedding_recovery_gap_count(seeded_connection) == 0


def test_recovery_lease_replaces_existing_grace_timer(tmp_path: Path):
    with mock.patch.object(config, "DAEMON_SHUTDOWN_GRACE_PERIOD_S", 3600):
        state = _DaemonState(str(tmp_path / "timer.db"), "test-key", False)
        assert state._acquire_inflight()
        state.start_initial_grace_period()
        timer_a = state._shutdown_timer
        assert timer_a is not None
        timer_b = None
        try:
            state._release_inflight()
            timer_b = state._shutdown_timer
            assert timer_b is not None
            assert timer_a is not timer_b
            assert timer_a.finished.is_set()
            assert not timer_b.finished.is_set()
        finally:
            if timer_b is not None:
                timer_b.cancel()
