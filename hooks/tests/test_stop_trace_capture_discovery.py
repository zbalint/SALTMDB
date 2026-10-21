"""The trace-capture hook validates daemon discovery before opening loopback RPC."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

HOOK_PATH = Path(__file__).resolve().parents[1] / "saltmdb-stop-trace-capture.py"
_SPEC = importlib.util.spec_from_file_location("trace_capture_discovery", HOOK_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load trace-capture hook")
hook = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hook)

PORT = "service_port"
TOKEN = "auth_token"
VALID_TOKEN = "tok"
VALID_PORT = 4000
REJECTED_FILE_MODES = (0o620, 0o602, 0o666)
ACCEPTED_FILE_MODES = (0o600, 0o644)
WINDOWS_PLATFORM = "win32"


@pytest.fixture
def daemon_file(tmp_path):
    home = tmp_path / "home"
    (home / ".saltmdb").mkdir(parents=True)
    db_path = tmp_path / "database.db"
    key = hashlib.sha256(os.path.realpath(db_path).encode("utf-8")).hexdigest()[:16]
    path = home / ".saltmdb" / f"daemon_{key}.json"

    def write_record(data):
        path.write_text(json.dumps(data), encoding="utf-8")
        path.chmod(0o600)

    return home, str(db_path), path, write_record


def test_daemon_accepts_valid_port_and_token(daemon_file):
    home, db_path, _path, write_record = daemon_file
    write_record({PORT: VALID_PORT, TOKEN: VALID_TOKEN})
    assert hook._daemon(home, db_path) == (VALID_PORT, VALID_TOKEN)


@pytest.mark.parametrize("port", ["4000", True, 0, 65536, -1, 1.5, "1\nFORGED"])
def test_daemon_rejects_invalid_port(daemon_file, port):
    home, db_path, _path, write_record = daemon_file
    write_record({PORT: port, TOKEN: VALID_TOKEN})
    assert hook._daemon(home, db_path) is None


@pytest.mark.parametrize("token", [7, "", "a" * 257, "a b", "a\nb", "caf\u00e9"])
def test_daemon_rejects_invalid_token(daemon_file, token):
    home, db_path, _path, write_record = daemon_file
    write_record({PORT: VALID_PORT, TOKEN: token})
    assert hook._daemon(home, db_path) is None


def test_daemon_rejects_missing_token(daemon_file):
    home, db_path, _path, write_record = daemon_file
    write_record({PORT: VALID_PORT})
    assert hook._daemon(home, db_path) is None


@pytest.mark.skipif(sys.platform == WINDOWS_PLATFORM, reason="requires POSIX file ownership")
def test_daemon_rejects_foreign_owner(daemon_file, monkeypatch):
    home, db_path, path, write_record = daemon_file
    write_record({PORT: VALID_PORT, TOKEN: VALID_TOKEN})
    assert hook._daemon(home, db_path) == (VALID_PORT, VALID_TOKEN)
    monkeypatch.setattr(hook.os, "getuid", lambda: path.stat().st_uid + 1)
    assert hook._daemon(home, db_path) is None


@pytest.mark.parametrize("mode", REJECTED_FILE_MODES)
@pytest.mark.skipif(sys.platform == WINDOWS_PLATFORM, reason="requires POSIX file modes")
def test_daemon_rejects_writable_file(daemon_file, mode):
    home, db_path, path, write_record = daemon_file
    write_record({PORT: VALID_PORT, TOKEN: VALID_TOKEN})
    path.chmod(mode)
    assert hook._daemon(home, db_path) is None


@pytest.mark.parametrize("mode", ACCEPTED_FILE_MODES)
@pytest.mark.skipif(sys.platform == WINDOWS_PLATFORM, reason="requires POSIX file modes")
def test_daemon_accepts_nonwritable_file(daemon_file, mode):
    home, db_path, path, write_record = daemon_file
    write_record({PORT: VALID_PORT, TOKEN: VALID_TOKEN})
    path.chmod(mode)
    assert hook._daemon(home, db_path) == (VALID_PORT, VALID_TOKEN)
