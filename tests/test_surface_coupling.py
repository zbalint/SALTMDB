from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
CLI = Path(
    os.environ.get(
        "S5_CLI", str(Path(__file__).resolve().parents[1] / "scripts/check_surface_coupling.py")
    )
)
from surface_scan import (  # noqa: E402
    Finding,
    Vocabulary,
    build_vocabulary,
    scan_paths,
    scan_text,
)
from surface_vocab import ALLOWED  # noqa: E402


@pytest.fixture(scope="module")
def vocabulary():
    return build_vocabulary()


def reasons(findings: list[Finding]) -> list[str]:
    return [finding.reason for finding in findings]


def test_real_tree_is_clean(vocabulary):
    paths = [
        "README.md",
        "INSTALL.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "skills",
        "hooks",
        "docs/architecture.md",
    ]
    assert scan_paths(paths, vocabulary, ALLOWED) == []


def test_synthetic_scanner_rules_report_file_and_line(vocabulary, tmp_path):
    text = (
        "mcp__saltmdb__not_registered\n"
        "mcp__saltmdb__not_registered*\n"
        'search_memory(query_keywords="x")\n'
        "`inspect_memory`\n"
        "`search_memory`\n"
    )
    path = tmp_path / "fixture.md"
    path.write_text(text)
    settings = tmp_path / "fixture.json"
    settings.write_text('{"type":"mcp_tool","tool":"not_registered","input":{"query":"x"}}\n')
    findings = scan_paths([str(path), str(settings)], vocabulary, ())
    assert all(finding.path.endswith(("fixture.md", "fixture.json")) for finding in findings)
    assert {finding.line for finding in findings} >= {1, 3, 4}
    assert any("unknown tool name" in reason for reason in reasons(findings))
    assert any("invalid field" in reason for reason in reasons(findings))
    assert any("removed public token" in reason for reason in reasons(findings))
    assert any("settings tool" in reason for reason in reasons(findings))
    assert not any(finding.line == 2 for finding in findings)
    assert not any(
        finding.line == 5 and "removed public token" in finding.reason for finding in findings
    )


def test_scanner_handles_depth_quotes_and_bare_names(vocabulary):
    text = (
        "manage_relation(relations=[{source_id: 'a', target_id: 'b'}])\n"
        "store_memory(content=\"a=b\", metadata={'entity_id': 'column'})\n"
        "entity_id is a database column\n"
    )
    assert scan_text("fixture.md", text, vocabulary, ()) == []


def test_argument_parser_stops_at_newline(vocabulary):
    text = 'search_memory(\n    query_keywords="x")\n'
    assert scan_text("fixture.md", text, vocabulary, ()) == []


def test_argument_parser_closes_quotes_before_later_fields(vocabulary):
    text = 'store_memory(content=")", definitely_invalid_field="x")\n'
    findings = scan_text("fixture.md", text, vocabulary, ())
    assert any(
        "invalid field for store_memory: definitely_invalid_field" in finding.reason
        for finding in findings
    )


def test_allow_list_pair_is_accepted_and_stale_entry_fails(vocabulary):
    text = "`query_keywords` is documented because the service function `memory_service.search_memory` keeps the name\n"
    query_allow = ALLOWED[:1]
    assert scan_text("docs/architecture.md", text, vocabulary, query_allow) == []
    stale = (("docs/architecture.md", "query_keywords", "missing context", "stale"),)
    findings = scan_text("docs/architecture.md", text, vocabulary, stale)
    assert any("allow-list entry did not match" in finding.reason for finding in findings)


def test_depth_one_argument_rules(vocabulary):
    text = (
        'search_memory("query_keywords": "x", query=[{"query_keywords": "nested", nested_field=1}, '
        "callback(field=1)], tags_filter=[{'query_keywords': 'nested'}])\n"
    )
    findings = scan_text("fixture.md", text, vocabulary, ())
    assert len(findings) == 1
    assert findings[0].reason == "invalid field for search_memory: query_keywords"


def test_qualified_names_and_suffixes_are_not_scanned(vocabulary, tmp_path):
    text = "memory_service.search_memory(foo=1)\n"
    assert scan_text("fixture.md", text, vocabulary, ()) == []
    assert scan_text("fixture.md", "xsearch_memory(foo=1)\n", vocabulary, ()) == []
    ignored = tmp_path / "fixture.txt"
    ignored.write_text("mcp__saltmdb__not_registered\n")
    assert scan_paths([str(ignored)], vocabulary, ()) == []


def test_exclusions_are_relative_to_cli_root(vocabulary, tmp_path):
    for dirname in ("src", "scripts"):
        root = tmp_path / dirname
        root.mkdir()
        stale = root / "fixture.md"
        stale.write_text("mcp__saltmdb__not_registered\n")
        findings = scan_paths([str(root)], vocabulary, ())
        assert any("unknown tool name" in finding.reason for finding in findings)
    nested = tmp_path / "project" / "tests"
    nested.mkdir(parents=True)
    (nested / "fixture.md").write_text("mcp__saltmdb__not_registered\n")
    assert scan_paths([str(tmp_path / "project")], vocabulary, ()) == []
    excluded_root = tmp_path / "excluded"
    (excluded_root / "docs" / "specs").mkdir(parents=True)
    (excluded_root / "docs" / "specs" / "fixture.md").write_text("mcp__saltmdb__not_registered\n")
    assert scan_paths([str(excluded_root)], vocabulary, ()) == []


def test_old_field_is_rejected_even_if_synthetic_schema_readds_it():
    from saltmdb.mcp.guard import RENAMED_FIELDS

    tool, fields = next(iter(RENAMED_FIELDS.items()))
    old_field = next(iter(fields))
    synthetic = Vocabulary({tool: frozenset({old_field})}, frozenset(), {}, frozenset())
    findings = scan_text(f"{tool}.md", f"{tool}({old_field}=1)\n", synthetic, ())
    assert any("invalid field" in finding.reason for finding in findings)


def test_allow_list_is_keyed_by_context_and_path(vocabulary, tmp_path):
    text = (
        "`query_keywords` is documented because the service function `memory_service.search_memory` keeps the name\n"
        "`query_keywords` appears in another context\n"
    )
    allow = (
        "docs/architecture.md",
        "query_keywords",
        "service function `memory_service.search_memory` keeps",
        "context exception",
    )
    findings = scan_text("docs/architecture.md", text, vocabulary, (allow,))
    assert len(findings) == 1
    absolute = tmp_path / "docs" / "architecture.md"
    absolute.parent.mkdir()
    absolute.write_text(text)
    absolute_findings = scan_paths([str(absolute)], vocabulary, (allow,))
    assert len(absolute_findings) == 1
    assert absolute_findings[0].line == 2


def test_stale_allow_entry_is_checked_by_scan_paths_and_cli(tmp_path, capsys):
    from check_surface_coupling import main

    path = tmp_path / "fixture.md"
    path.write_text("query_keywords appears without the allowed context\n")
    stale = (("fixture.md", "query_keywords", "missing context", "stale"),)
    findings = scan_paths([str(path)], vocabulary=build_vocabulary(), allow_list=stale)
    assert any("allow-list entry did not match" in finding.reason for finding in findings)
    assert main([str(path)], stale) == 1
    assert "allow-list entry did not match" in capsys.readouterr().out


def test_vocabulary_covers_guard_maps(vocabulary):
    from saltmdb.mcp.guard import REMOVED_TOOLS, RENAMED_FIELDS

    assert set(RENAMED_FIELDS) <= set(vocabulary.registered_tools) | set(vocabulary.removed_tools)
    assert set(REMOVED_TOOLS) <= set(vocabulary.removed_tools)
    assert set(vocabulary.removed_fields) <= set(vocabulary.registered_tools)


def test_cli_reports_findings_and_clean_directory(tmp_path):
    dirty = tmp_path / "dirty.md"
    dirty.write_text("mcp__saltmdb__not_registered\n")
    result = subprocess.run(
        [sys.executable, CLI, str(dirty)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "dirty.md:" in result.stdout

    clean = tmp_path / "clean"
    clean.mkdir()
    result = subprocess.run(
        [sys.executable, CLI, str(clean)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_cli_path_abuse_cases(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("mcp__saltmdb__not_registered\n")
    root = tmp_path / "root"
    root.mkdir()
    nested_link = root / "nested.md"
    nested_link.symlink_to(outside)
    dirty = root / "dirty.md"
    dirty.write_text("mcp__saltmdb__not_registered\n")
    root_link = tmp_path / "root-link"
    root_link.symlink_to(root, target_is_directory=True)
    result = subprocess.run(
        [sys.executable, CLI, str(root_link)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "dirty.md:" in result.stdout
    assert "outside.md" not in result.stdout
    root_file = tmp_path / "root-file.md"
    root_file.write_text("mcp__saltmdb__not_registered\n")
    root_file_link = tmp_path / "root-file-link.md"
    root_file_link.symlink_to(root_file)
    result = subprocess.run(
        [sys.executable, CLI, str(root_file_link)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "root-file.md:" in result.stdout

    too_large = root / "large.md"
    too_large.write_bytes(b"x" * (1024 * 1024 + 1))
    result = subprocess.run(
        [sys.executable, CLI, str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "skipped: too large" in result.stdout

    invalid = root / "invalid.md"
    invalid.write_bytes(b"\xff")
    result = subprocess.run(
        [sys.executable, CLI, str(invalid)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_cli_truncates_matched_line(tmp_path):
    path = tmp_path / "long.md"
    path.write_text("mcp__saltmdb__not_registered " + "x" * 400)
    result = subprocess.run(
        [sys.executable, CLI, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert all(len(line) <= 200 for line in result.stdout.splitlines())
