"""Release metadata must identify the same SALTMDB version everywhere."""

from __future__ import annotations

import re
from pathlib import Path

from saltmdb import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_version_matches_pyproject():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', pyproject, flags=re.MULTILINE)

    assert match is not None
    assert __version__ == match.group(1)


def test_lockfile_version_matches_project_version():
    lockfile = (ROOT / "uv.lock").read_text(encoding="utf-8")
    saltmdb_block = re.search(r'\[\[package\]\]\nname = "saltmdb"\nversion = "([^"]+)"', lockfile)

    assert saltmdb_block is not None
    assert saltmdb_block.group(1) == __version__.replace("-alpha.", "a")
