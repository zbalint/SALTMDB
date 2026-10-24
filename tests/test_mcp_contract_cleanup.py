from __future__ import annotations

import concurrent.futures
import tempfile
import time
from pathlib import Path
from threading import Event

import pytest
import mcp_contract
from saltmdb.domain.services import event_service, librarian_service


def _enable_librarian(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SALTMDB_DISABLE_LIBRARIAN", raising=False)
    monkeypatch.delenv("SALTMDB_TEST_MODE", raising=False)
    monkeypatch.setattr(librarian_service.trigger_librarian, "disabled", False, raising=False)


def test_cleanup_waits_for_librarian_before_removing_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_librarian(monkeypatch)
    with tempfile.TemporaryDirectory() as temp_name:
        temp_path = Path(temp_name)
        marker = temp_path / "maintenance-marker"
        started = Event()
        finished = Event()
        connection, previous_backend, previous_db_path = mcp_contract.configure(
            temp_path / "s4d.db"
        )

        def slow_maintenance(_db_path: str, _force: bool) -> str:
            started.set()
            time.sleep(0.2)
            marker.write_text("complete")
            finished.set()
            return "complete"

        monkeypatch.setattr(librarian_service, "_run_maintenance_pass_impl", slow_maintenance)
        try:
            result = event_service.log_event(
                agent_id="cleanup-test",
                type="issue",
                content="trigger librarian maintenance",
                db_connection=connection,
            )
            assert result["status"] == "ok"
            assert started.wait(timeout=5)
            mcp_contract.cleanup(connection, previous_backend, previous_db_path)
            assert finished.is_set()
            assert marker.read_text() == "complete"
        finally:
            if not finished.is_set():
                librarian_service._librarian_trigger_pool.submit(lambda: None).result(timeout=30)
            if marker.exists():
                assert marker.read_text() == "complete"


def test_drain_librarian_pool_returns_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_librarian(monkeypatch)
    mcp_contract.drain_librarian_pool()
    started = time.monotonic()
    mcp_contract.drain_librarian_pool()
    assert time.monotonic() - started < 1.0


def test_drain_librarian_pool_timeout_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_librarian(monkeypatch)
    with tempfile.TemporaryDirectory() as temp_name:
        temp_path = Path(temp_name)
        started = Event()
        release = Event()
        connection, previous_backend, previous_db_path = mcp_contract.configure(
            temp_path / "s4d.db"
        )

        def blocked_maintenance(_db_path: str, _force: bool) -> str:
            started.set()
            release.wait()
            return "released"

        monkeypatch.setattr(librarian_service, "_run_maintenance_pass_impl", blocked_maintenance)
        try:
            result = event_service.log_event(
                agent_id="cleanup-test",
                type="issue",
                content="trigger blocked maintenance",
                db_connection=connection,
            )
            assert result["status"] == "ok"
            assert started.wait(timeout=5)
            with pytest.raises(concurrent.futures.TimeoutError):
                mcp_contract.drain_librarian_pool(timeout=0.01)
        finally:
            release.set()
            mcp_contract.cleanup(connection, previous_backend, previous_db_path)
