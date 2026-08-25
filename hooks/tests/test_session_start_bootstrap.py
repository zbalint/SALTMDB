"""Tests for saltmdb-session-start-bootstrap.py's CLI-executable resolution.

Covers the fix for a real portability bug: the script used to hardcode
``~/.mcp/SALTMDB/.venv/bin/saltmdb-cli`` as the *only* place it would look for
``saltmdb-cli``, silently emitting nothing (no bootstrap digest, no error) for anyone who
installed SALTMDB anywhere else. ``_resolve_cli()`` now tries, most explicit first:
``SALTMDB_CLI_PATH`` env var, ``saltmdb-cli`` on PATH, then that original layout as a
last-resort fallback only.
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path
from unittest.mock import patch

HOOKS_DIR = Path(__file__).resolve().parent.parent


def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, HOOKS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_fake_cli(tmp_path: Path) -> Path:
    cli = tmp_path / "saltmdb-cli"
    cli.write_text("#!/bin/sh\necho fake\n")
    cli.chmod(0o755)
    return cli


def test_env_var_override_wins_when_file_exists():
    bootstrap = _load_module("saltmdb-session-start-bootstrap")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _make_fake_cli(Path(tmp))
        with patch.dict("os.environ", {"SALTMDB_CLI_PATH": str(cli)}, clear=False):
            with patch("shutil.which", return_value="/should/not/be/used"):
                assert bootstrap._resolve_cli() == str(cli)


def test_env_var_ignored_when_pointing_at_nonexistent_file():
    bootstrap = _load_module("saltmdb-session-start-bootstrap")
    with patch.dict("os.environ", {"SALTMDB_CLI_PATH": "/no/such/file"}, clear=False):
        with patch("shutil.which", return_value="/usr/local/bin/saltmdb-cli"):
            assert bootstrap._resolve_cli() == "/usr/local/bin/saltmdb-cli"


def test_falls_back_to_path_lookup_when_no_env_override():
    bootstrap = _load_module("saltmdb-session-start-bootstrap")
    with patch.dict("os.environ", {}, clear=True):
        with patch("shutil.which", return_value="/opt/saltmdb/bin/saltmdb-cli"):
            assert bootstrap._resolve_cli() == "/opt/saltmdb/bin/saltmdb-cli"


def test_falls_back_to_legacy_default_when_nothing_else_found():
    bootstrap = _load_module("saltmdb-session-start-bootstrap")
    with tempfile.TemporaryDirectory() as tmp:
        fake_home = Path(tmp)
        legacy = fake_home / ".mcp" / "SALTMDB" / ".venv" / "bin"
        legacy.mkdir(parents=True)
        cli = legacy / "saltmdb-cli"
        cli.write_text("#!/bin/sh\necho fake\n")
        with patch.dict("os.environ", {}, clear=True):
            with patch("shutil.which", return_value=None):
                with patch.object(bootstrap.Path, "home", return_value=fake_home):
                    assert bootstrap._resolve_cli() == str(cli)


def test_returns_none_when_nothing_found_anywhere():
    bootstrap = _load_module("saltmdb-session-start-bootstrap")
    with tempfile.TemporaryDirectory() as tmp:
        fake_home = Path(tmp)  # empty -- no legacy install here either
        with patch.dict("os.environ", {}, clear=True):
            with patch("shutil.which", return_value=None):
                with patch.object(bootstrap.Path, "home", return_value=fake_home):
                    assert bootstrap._resolve_cli() is None


def test_run_cli_returns_none_without_a_subprocess_call_when_unresolved():
    bootstrap = _load_module("saltmdb-session-start-bootstrap")
    bootstrap.CLI = None
    with patch("subprocess.run") as mock_run:
        assert bootstrap.run_cli("bootstrap-digest") is None
        mock_run.assert_not_called()


def test_main_prints_session_digest_when_run_cli_returns_one(capsys):
    bootstrap = _load_module("saltmdb-session-start-bootstrap")
    fake_session_digest = "<saltmdb-last-session-digest>\n\n</saltmdb-last-session-digest>"

    def fake_run_cli(*args):
        if args and args[0] == "session-digest":
            return fake_session_digest
        return None

    with patch.object(bootstrap, "run_cli", side_effect=fake_run_cli):
        bootstrap.main()
    assert fake_session_digest in capsys.readouterr().out


def test_main_skips_session_digest_section_when_run_cli_returns_none(capsys):
    bootstrap = _load_module("saltmdb-session-start-bootstrap")

    with patch.object(bootstrap, "run_cli", return_value=None):
        bootstrap.main()
    assert "saltmdb-last-session-digest" not in capsys.readouterr().out
