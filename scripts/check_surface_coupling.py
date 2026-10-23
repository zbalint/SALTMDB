#!/usr/bin/env python3
"""Run the public-surface coupling scanner over supplied files or roots."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _load_scanner():
    """Import the scanner and the allow-list from their repository locations."""
    sys.path[:0] = [str(SCRIPT_DIR), str(SCRIPT_DIR.parent / "tests")]
    from surface_scan import build_vocabulary, scan_paths
    from surface_vocab import ALLOWED

    return build_vocabulary, scan_paths, ALLOWED


def main(argv: list[str] | None = None, allow_list=None) -> int:
    paths = argv if argv is not None else sys.argv[1:]
    if not paths:
        print("usage: check_surface_coupling.py PATH...", file=sys.stderr)
        return 2
    build_vocabulary, scan_paths, allowed = _load_scanner()
    findings = scan_paths(paths, build_vocabulary(), allowed if allow_list is None else allow_list)
    for finding in findings:
        rendered = f"{finding.path}:{finding.line}: {finding.reason}"
        if finding.text:
            rendered += f": {finding.text}"
        print(rendered[:200])
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
