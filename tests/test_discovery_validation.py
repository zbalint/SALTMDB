"""Discovery records are validated before they reach sockets or log messages."""

import json
import logging
import os
from pathlib import Path
import stat
import sys
from unittest.mock import MagicMock

import pytest

from saltmdb import config
from saltmdb.daemon import client, discovery

PORT = "service_port"
TOKEN = "auth_token"
PID = "daemon_pid"
DB_PATH = "db_path"
SCHEMA_VERSION = "schema_version"
STARTED_AT = "started_at"
WRITE_STARTED_AT = "2026-10-21T08:24:25.341482+00:00"
NON_CANONICAL_STARTED_AT = "2026-10-21 08:24:25+00:00"
EXTRA = "extra"
DISCOVERY_KEY = "validation"
HOME_ENV = "HOME"
USERPROFILE_ENV = "USERPROFILE"
VALID_TOKEN = "tok"
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX file modes/uid")
REJECTED_FILE_MODES = (0o620, 0o602, 0o666)
ACCEPTED_FILE_MODES = (0o600, 0o644)


@pytest.fixture
def record_file(tmp_path, monkeypatch):
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    monkeypatch.setenv(USERPROFILE_ENV, str(tmp_path))
    path = Path(discovery.discovery_path(DISCOVERY_KEY))

    def write_record(data):
        path.write_text(json.dumps(data), encoding="utf-8")
        path.chmod(0o600)
        return path

    return write_record


@pytest.mark.parametrize("port", [1, 4000, 65535])
def test_read_accepts_valid_record(record_file, port):
    data = {PORT: port, TOKEN: VALID_TOKEN, PID: 7, DB_PATH: "/fictional.db", "extra": []}
    record_file(data)
    assert discovery.read(DISCOVERY_KEY) == {
        PORT: port,
        TOKEN: VALID_TOKEN,
        PID: 7,
        DB_PATH: "/fictional.db",
    }


@pytest.mark.parametrize("port", ["4000", True, 0, 65536, -1, 1.5, "1\nFORGED"])
def test_read_rejects_invalid_port(record_file, port):
    record_file({PORT: port, TOKEN: VALID_TOKEN})
    assert discovery.read(DISCOVERY_KEY) is None


@pytest.mark.parametrize("token", [7, "", "a" * 257, "a b", "a\nb", "caf\u00e9"])
def test_read_rejects_invalid_token(record_file, token):
    record_file({PORT: 4000, TOKEN: token})
    assert discovery.read(DISCOVERY_KEY) is None


def test_read_rejects_missing_token(record_file):
    record_file({PORT: 4000})
    assert discovery.read(DISCOVERY_KEY) is None


@pytest.mark.parametrize("pid", ["7", 0, -1, True])
def test_read_rejects_invalid_pid(record_file, pid):
    record_file({PORT: 4000, TOKEN: VALID_TOKEN, PID: pid})
    assert discovery.read(DISCOVERY_KEY) is None


def test_read_rejects_nonstring_db_path(record_file):
    record_file({PORT: 4000, TOKEN: VALID_TOKEN, DB_PATH: 7})
    assert discovery.read(DISCOVERY_KEY) is None


def test_read_rejects_json_list(record_file):
    record_file([4000, VALID_TOKEN])
    assert discovery.read(DISCOVERY_KEY) is None


def test_read_returns_loaded_dict_identity(record_file, monkeypatch):
    sentinel = {PORT: 4000, TOKEN: VALID_TOKEN}
    record_file(sentinel)
    monkeypatch.setattr(json, "load", lambda _handle: sentinel)
    record = discovery.read(DISCOVERY_KEY)
    assert record is not sentinel
    assert record == {PORT: 4000, TOKEN: VALID_TOKEN}


def test_read_rejects_non_utf8_file(record_file):
    valid = {PORT: 4000, TOKEN: VALID_TOKEN}
    path = record_file(valid)
    assert discovery.read(DISCOVERY_KEY) == valid
    path.write_bytes(b'\xff\xfe\x80{"service_port": 4000}')
    assert discovery.read(DISCOVERY_KEY) is None


@pytest.mark.parametrize("token", ["!~", "a" * 256])
def test_read_accepts_token_boundaries_without_optional_fields(record_file, token):
    record_file({PORT: 4000, TOKEN: token})
    assert discovery.read(DISCOVERY_KEY) == {PORT: 4000, TOKEN: token}


@pytest.mark.parametrize("mode", REJECTED_FILE_MODES)
@POSIX_ONLY
def test_read_rejects_writable_file_and_warns_once(record_file, caplog, mode):
    path = record_file({PORT: 4000, TOKEN: VALID_TOKEN})
    path.chmod(mode)
    with caplog.at_level(logging.WARNING, logger=discovery.__name__):
        assert discovery.read(DISCOVERY_KEY) is None
        assert discovery.read(DISCOVERY_KEY) is None
    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage() == (
        f"Discovery file {path} is not owned by the current user or writable by group/others"
    )


@pytest.mark.parametrize("mode", ACCEPTED_FILE_MODES)
@POSIX_ONLY
def test_read_accepts_nonwritable_file(record_file, mode):
    data = {PORT: 4000, TOKEN: VALID_TOKEN}
    path = record_file(data)
    path.chmod(mode)
    assert discovery.read(DISCOVERY_KEY) == data


@POSIX_ONLY
def test_read_rejects_foreign_owner(record_file, monkeypatch):
    path = record_file({PORT: 4000, TOKEN: VALID_TOKEN})
    monkeypatch.setattr(discovery.os, "getuid", lambda: path.stat().st_uid + 1)
    assert discovery.read(DISCOVERY_KEY) is None


def test_invalid_record_never_reaches_socket(record_file, monkeypatch, tmp_path):
    db_path = str(tmp_path / "untrusted.db")
    path = record_file({PORT: "1\nFORGED", TOKEN: VALID_TOKEN, DB_PATH: db_path})
    monkeypatch.setattr(discovery, "discovery_path", lambda _key: str(path))
    connect = MagicMock(side_effect=AssertionError("invalid discovery reached network"))
    monkeypatch.setattr(client.socket, "create_connection", connect)
    assert client.reachable_daemon_info(db_path) is None
    connect.assert_not_called()


@pytest.mark.parametrize("creator", [discovery._discovery_dir, config.get_db_path])
@POSIX_ONLY
def test_new_directory_is_private(tmp_path, monkeypatch, creator):
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    monkeypatch.setenv(USERPROFILE_ENV, str(tmp_path))
    old_umask = os.umask(0o022)
    try:
        creator()
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE((tmp_path / ".saltmdb").stat().st_mode) == 0o700


@pytest.mark.parametrize("creator", [discovery._discovery_dir, config.get_db_path])
@POSIX_ONLY
def test_existing_directory_mode_is_preserved(tmp_path, monkeypatch, creator):
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    monkeypatch.setenv(USERPROFILE_ENV, str(tmp_path))
    directory = tmp_path / ".saltmdb"
    directory.mkdir()
    directory.chmod(0o755)
    creator()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755


@pytest.fixture
def reachable_float_record(monkeypatch):
    responses = iter([None, {PORT: 4000.0, PID: 7.0, TOKEN: VALID_TOKEN}])
    monkeypatch.setattr(client, "reachable_daemon_info", lambda _path: next(responses))
    monkeypatch.setattr(client, "probe_owner", lambda _path: "ready")
    monkeypatch.setattr(client.time, "sleep", lambda _delay: None)


@pytest.mark.usefixtures("reachable_float_record")
def test_startup_log_formats_numeric_discovery(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger=client.__name__):
        client.ensure_daemon_running(str(tmp_path / "logging.db"))
    record = caplog.records[-1]
    assert record.msg == "ensure_daemon_running: daemon reachable after %.2f s (pid=%d port=%d)"
    assert all(type(arg) is int for arg in record.args[-2:])
    assert record.getMessage().endswith("(pid=7 port=4000)")
    assert "4000.0" not in caplog.text


@pytest.mark.usefixtures("reachable_float_record")
def test_session_log_formats_numeric_discovery(tmp_path, monkeypatch, caplog):
    sock = MagicMock()
    monkeypatch.setattr(client.socket, "create_connection", lambda *_args, **_kwargs: sock)
    monkeypatch.setattr(client.protocol, "send_frame", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(client.protocol, "recv_frame", lambda _sock: {"ok": True})
    monkeypatch.setattr(client, "_current_session", None)
    session = client.SessionConnection(str(tmp_path / "session.db"))
    try:
        with caplog.at_level(logging.INFO, logger=client.__name__):
            session.open()
        record = caplog.records[-1]
        assert record.msg == (
            "Session opened: agent_session_id=%s adapter_pid=%d daemon_pid=%d service_port=%d"
        )
        assert all(type(arg) is int for arg in record.args[1:])
        assert record.getMessage().endswith("daemon_pid=7 service_port=4000")
        assert "4000.0" not in caplog.text
    finally:
        session.close(send_goodbye=False)


def test_read_keeps_present_optional_fields_with_exact_types(record_file):
    data = {
        PORT: 4000,
        TOKEN: VALID_TOKEN,
        PID: 7,
        DB_PATH: "/fictional.db",
        SCHEMA_VERSION: 1,
        EXTRA: [],
    }
    record_file(data)
    record = discovery.read(DISCOVERY_KEY)
    assert record is not None
    assert record == {
        PORT: 4000,
        TOKEN: VALID_TOKEN,
        PID: 7,
        DB_PATH: "/fictional.db",
    }
    assert type(record[PORT]) is int
    assert type(record[TOKEN]) is str
    assert type(record[PID]) is int
    assert type(record[DB_PATH]) is str


def test_read_does_not_add_absent_optional_fields(record_file):
    record_file({PORT: 4000, TOKEN: VALID_TOKEN})
    record = discovery.read(DISCOVERY_KEY)
    assert record is not None
    assert PID not in record
    assert DB_PATH not in record
    assert STARTED_AT not in record


def test_read_keeps_write_shaped_started_at(record_file):
    record_file({PORT: 4000, TOKEN: VALID_TOKEN, STARTED_AT: WRITE_STARTED_AT})
    assert discovery.read(DISCOVERY_KEY) == {
        PORT: 4000,
        TOKEN: VALID_TOKEN,
        STARTED_AT: WRITE_STARTED_AT,
    }


def test_read_normalizes_noncanonical_started_at(record_file):
    record_file({PORT: 4000, TOKEN: VALID_TOKEN, STARTED_AT: NON_CANONICAL_STARTED_AT})
    assert discovery.read(DISCOVERY_KEY) == {
        PORT: 4000,
        TOKEN: VALID_TOKEN,
        STARTED_AT: "2026-10-21T08:24:25+00:00",
    }


@pytest.mark.parametrize("started_at", ["not-a-date", 7, "\x00" + "2026-10-21T08:24:25+00:00"])
def test_read_drops_invalid_started_at(record_file, started_at):
    record_file({PORT: 4000, TOKEN: VALID_TOKEN, STARTED_AT: started_at})
    record = discovery.read(DISCOVERY_KEY)
    assert record is not None
    assert STARTED_AT not in record


def test_read_converts_subclassed_required_values(record_file, monkeypatch):
    class _S(str):
        pass

    class _I(int):
        pass

    data = {
        PORT: _I(4000),
        TOKEN: _S(VALID_TOKEN),
        PID: _I(7),
        DB_PATH: _S("/fictional.db"),
    }
    record_file(data)
    monkeypatch.setattr(json, "load", lambda _handle: data)
    record = discovery.read(DISCOVERY_KEY)
    assert record is not None
    assert type(record[PORT]) is int
    assert type(record[TOKEN]) is str
    assert type(record[PID]) is int
    assert type(record[DB_PATH]) is str


def test_read_returns_independent_records(record_file):
    record_file({PORT: 4000, TOKEN: VALID_TOKEN})
    first = discovery.read(DISCOVERY_KEY)
    second = discovery.read(DISCOVERY_KEY)
    assert first is not None
    assert second is not None
    assert first is not second
    first[PORT] = 5000
    assert second[PORT] == 4000


def test_reachable_daemon_info_returns_typed_record_without_extras(
    record_file, monkeypatch, tmp_path
):
    db_path = str(tmp_path / "reachable.db")
    record_file(
        {
            PORT: 4000,
            TOKEN: VALID_TOKEN,
            DB_PATH: db_path,
            SCHEMA_VERSION: 1,
            EXTRA: [],
        }
    )
    monkeypatch.setattr(client.discovery, "daemon_key", lambda _path: DISCOVERY_KEY)
    monkeypatch.setattr(client, "_authenticated_ping_ok", lambda _info: True)
    assert client.reachable_daemon_info(db_path) == {
        PORT: 4000,
        TOKEN: VALID_TOKEN,
        DB_PATH: db_path,
    }
