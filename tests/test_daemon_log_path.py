import logging
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import IO
from unittest.mock import MagicMock, patch
import pytest

from saltmdb import config
from saltmdb.daemon import client, discovery, server, service


_CANONICAL_DB = "/database-owner/private-store/records.db"
_OTHER_DB = "/database-owner/private-store/other.db"
_HOSTILE_DB = "/database-owner/../private store/records\n.db"
_LOG_NAME = "daemon.log"
_ENCODING = "utf-8"
_LOG_DIR_MODE = 0o700
_LOG_FILE_MODE = 0o600
_HOME_DIR_NAME = ".saltmdb"
_LOGS_DIR_NAME = "logs"


def _expected_log_path(home: Path, canonical_db_path: str) -> Path:
    return (
        home / _HOME_DIR_NAME / _LOGS_DIR_NAME / discovery.daemon_key(canonical_db_path) / _LOG_NAME
    )


def _record_opened_log(
    handles: list[IO[str]], opener: Callable[[str], IO[str]] = discovery.open_daemon_log
) -> Callable[[str], IO[str]]:
    def open_log(canonical_db_path: str):
        handle = opener(canonical_db_path)
        handles.append(handle)
        return handle

    return open_log


@pytest.fixture
def noncanonical_db_path(tmp_path: Path) -> tuple[str, str]:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_db = real_dir / "database.sqlite"
    real_db.touch()
    alias_dir = tmp_path / "alias"
    alias_dir.symlink_to(real_dir, target_is_directory=True)
    raw_db = alias_dir / "database.sqlite"
    return str(raw_db), os.path.realpath(raw_db)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def test_log_path_uses_database_key_under_home(tmp_path: Path):
    expected = tmp_path / ".saltmdb" / "logs" / discovery.daemon_key(_CANONICAL_DB) / _LOG_NAME
    actual = Path(discovery.daemon_log_path(_CANONICAL_DB))
    assert actual == expected
    assert set(Path(_CANONICAL_DB).parts[1:]).isdisjoint(actual.parts)


def test_different_canonical_databases_get_distinct_log_paths():
    assert discovery.daemon_log_path(_CANONICAL_DB) != discovery.daemon_log_path(_OTHER_DB)


def test_hostile_database_path_stays_under_log_root(tmp_path: Path):
    root = tmp_path / ".saltmdb" / "logs"
    actual = Path(discovery.daemon_log_path(_HOSTILE_DB))
    assert actual.is_relative_to(root)
    assert "private store" not in actual.parts
    assert "records\n.db" not in actual.parts


def test_open_daemon_log_sets_modes_and_preserves_existing_directory_mode():
    previous_umask = os.umask(0o022)
    try:
        with discovery.open_daemon_log(_CANONICAL_DB) as handle:
            path = Path(handle.name)
            directory = path.parent
        assert stat.S_IMODE(path.stat().st_mode) == _LOG_FILE_MODE
        assert stat.S_IMODE(directory.stat().st_mode) == _LOG_DIR_MODE
        directory.chmod(0o755)
        with discovery.open_daemon_log(_CANONICAL_DB) as handle:
            assert Path(handle.name) == path
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    finally:
        os.umask(previous_umask)


def test_open_daemon_log_appends_with_two_open_handles():
    first = discovery.open_daemon_log(_CANONICAL_DB)
    second = discovery.open_daemon_log(_CANONICAL_DB)
    try:
        first.write("A\n")
        first.flush()
        second.write("B\n")
        second.flush()
    finally:
        first.close()
        second.close()
    assert Path(discovery.daemon_log_path(_CANONICAL_DB)).read_text(encoding=_ENCODING) == "A\nB\n"


def test_client_tail_reads_new_log_and_missing_fallback():
    assert client._read_daemon_log_tail(_CANONICAL_DB) == "(no daemon.log found)"
    with discovery.open_daemon_log(_CANONICAL_DB) as handle:
        handle.write("first\nlast\n")
    assert client._read_daemon_log_tail(_CANONICAL_DB, lines=1) == "last\n"


def test_client_tail_canonicalizes_noncanonical_path(
    tmp_path: Path, noncanonical_db_path: tuple[str, str]
):
    raw_db, canonical_db = noncanonical_db_path
    expected = _expected_log_path(tmp_path, canonical_db)
    raw_expected = _expected_log_path(tmp_path, raw_db)
    with discovery.open_daemon_log(canonical_db) as handle:
        handle.write("canonical tail\n")
    assert client._read_daemon_log_tail(raw_db) == "canonical tail\n"
    assert expected.is_file()
    assert not raw_expected.exists()


def test_client_spawn_canonicalizes_noncanonical_path(
    tmp_path: Path, noncanonical_db_path: tuple[str, str]
):
    raw_db, canonical_db = noncanonical_db_path
    process = MagicMock(pid=123)
    with patch.object(client.subprocess, "Popen", return_value=process) as popen:
        client._spawn_daemon_process(raw_db)
    expected = _expected_log_path(tmp_path, canonical_db)
    raw_expected = _expected_log_path(tmp_path, raw_db)
    kwargs = popen.call_args.kwargs
    assert kwargs["stdout"].name == str(expected)
    assert kwargs["stderr"].name == str(expected)
    assert not raw_expected.exists()


def test_client_spawn_redirects_both_streams_to_new_log_path():
    process = MagicMock(pid=123)
    with patch.object(client.subprocess, "Popen", return_value=process) as popen:
        client._spawn_daemon_process(_CANONICAL_DB)
    kwargs = popen.call_args.kwargs
    expected = discovery.daemon_log_path(_CANONICAL_DB)
    assert kwargs["stdout"].name == expected
    assert kwargs["stderr"].name == expected


def test_intermediary_canonicalizes_and_closes_noncanonical_path(
    tmp_path: Path, noncanonical_db_path: tuple[str, str]
):
    raw_db, canonical_db = noncanonical_db_path
    root = logging.getLogger()
    opened: list[IO[str]] = []
    with (
        patch.object(root, "handlers", []),
        patch.object(
            discovery, "open_daemon_log", side_effect=_record_opened_log(opened)
        ) as open_log,
        patch.object(client.logging, "basicConfig"),
        patch.object(client, "_spawn_daemon_process"),
    ):
        client._intermediary_main(["client.py", "--spawn-detached", raw_db])
    expected = _expected_log_path(tmp_path, canonical_db)
    raw_expected = _expected_log_path(tmp_path, raw_db)
    open_log.assert_called_once_with(canonical_db)
    assert opened[0].closed
    assert expected.is_file()
    assert not raw_expected.exists()


def test_intermediary_closes_log_when_spawn_raises(
    tmp_path: Path, noncanonical_db_path: tuple[str, str]
):
    raw_db, canonical_db = noncanonical_db_path
    root = logging.getLogger()
    opened: list[IO[str]] = []
    with (
        patch.object(root, "handlers", []),
        patch.object(
            discovery, "open_daemon_log", side_effect=_record_opened_log(opened)
        ) as open_log,
        patch.object(client.logging, "basicConfig"),
        patch.object(client, "_spawn_daemon_process", side_effect=RuntimeError("spawn failed")),
        pytest.raises(RuntimeError, match="spawn failed"),
    ):
        client._intermediary_main(["client.py", "--spawn-detached", raw_db])
    expected = _expected_log_path(tmp_path, canonical_db)
    raw_expected = _expected_log_path(tmp_path, raw_db)
    open_log.assert_called_once_with(canonical_db)
    assert opened[0].closed
    assert expected.is_file()
    assert not raw_expected.exists()


def test_intermediary_does_not_open_log_with_root_handlers():
    root = logging.getLogger()
    with (
        patch.object(root, "handlers", [MagicMock()]),
        patch.object(discovery, "open_daemon_log") as open_log,
        patch.object(client, "_spawn_daemon_process"),
    ):
        client._intermediary_main(["client.py", "--spawn-detached", _CANONICAL_DB])
    open_log.assert_not_called()


def test_server_redirect_uses_new_log_path():
    old_stdout, old_stderr = server.sys.stdout, server.sys.stderr
    try:
        server._daemon_log_redirect(_CANONICAL_DB)
        assert server.sys.stdout.name == discovery.daemon_log_path(_CANONICAL_DB)
        assert server.sys.stderr is server.sys.stdout
    finally:
        log_handle = server.sys.stdout
        server.sys.stdout, server.sys.stderr = old_stdout, old_stderr
        log_handle.close()


def test_server_redirect_canonicalizes_noncanonical_path(
    tmp_path: Path, noncanonical_db_path: tuple[str, str]
):
    raw_db, canonical_db = noncanonical_db_path
    expected = _expected_log_path(tmp_path, canonical_db)
    raw_expected = _expected_log_path(tmp_path, raw_db)
    old_stdout, old_stderr = server.sys.stdout, server.sys.stderr
    try:
        server._daemon_log_redirect(raw_db)
        assert server.sys.stdout.name == str(expected)
        assert server.sys.stderr is server.sys.stdout
    finally:
        log_handle = server.sys.stdout
        server.sys.stdout, server.sys.stderr = old_stdout, old_stderr
        log_handle.close()
    assert expected.is_file()
    assert not raw_expected.exists()


def test_service_messages_name_new_log_path():
    canonical = discovery.resolve_canonical_db_path(_CANONICAL_DB)
    expected = discovery.daemon_log_path(canonical)
    assert expected in service._start_timeout_result(canonical).message
    with patch.object(service.client, "_spawn_daemon_process", side_effect=OSError("denied")):
        spawn_result = service._spawn_started_daemon(canonical, 0)
        assert spawn_result[0] is not None
        assert expected in spawn_result[0].message
    clock = MagicMock()
    clock.monotonic.side_effect = [0, config.DAEMON_STOP_WAIT_S + 1]
    with (
        patch.object(service, "time", clock),
        patch.object(service, "_read_proc_cmdline", return_value="saltmdb.daemon.server"),
    ):
        assert expected in service._wait_for_stopped_daemon(123, canonical).message
