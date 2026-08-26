"""CLI boundary tests for the read-only corpus snapshot export (agent API redesign plan §5.12,
Phase 7 item 29: export_corpus_snapshot moved off MCP entirely -- no tool, no dispatch entry,
no protocol classification. saltmdb.cli.cmd_export_corpus_snapshot is the sole surface now;
identity comes from the same SALTMDB_OWNER_ID environment setting as the MCP adapter."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from saltmdb import cli
from saltmdb.daemon import dispatch, protocol
from saltmdb.domain.services.corpus_snapshot_service import SnapshotChangedError
from saltmdb.mcp import tools


def _parse(argv):
    return cli.build_parser().parse_args(argv)


@pytest.fixture(autouse=True)
def _owner_env(monkeypatch):
    monkeypatch.setenv("SALTMDB_OWNER_ID", "snapshot-owner")


def _page(entities, *, entity_count, has_more, next_cursor):
    return {
        "entities": entities,
        "entity_count": entity_count,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "snapshot_hash": "a" * 64,
        "owner_id": "snapshot-owner",
    }


def test_export_corpus_snapshot_is_not_a_registered_mcp_tool():
    assert "export_corpus_snapshot" not in tools.mcp._tool_manager._tools
    assert not hasattr(tools, "export_corpus_snapshot")


def test_export_corpus_snapshot_is_not_in_dispatch_table_or_protocol():
    assert "export_corpus_snapshot" not in dispatch.DISPATCH_TABLE
    assert "export_corpus_snapshot" not in protocol.READ_TOOLS
    assert not hasattr(dispatch, "_dispatch_export_corpus_snapshot")


def test_cli_export_has_no_owner_id_argument():
    args = _parse(["export-corpus-snapshot"])
    assert not hasattr(args, "owner_id")
    with pytest.raises(SystemExit):
        _parse(["export-corpus-snapshot", "--owner-id", "someone"])


def test_cli_export_merges_pages_into_one_complete_document(capsys):
    pages = [
        _page([{"id": "e1"}, {"id": "e2"}], entity_count=3, has_more=True, next_cursor="e2"),
        _page([{"id": "e3"}], entity_count=3, has_more=False, next_cursor=None),
    ]
    args = _parse(["export-corpus-snapshot", "--page-size", "2"])
    with patch(
        "saltmdb.domain.services.corpus_snapshot_service.iter_corpus_snapshot_pages",
        return_value=iter(pages),
    ) as fn:
        rc = cli.cmd_export_corpus_snapshot(args)
    assert rc == 0
    call_kwargs = fn.call_args.kwargs
    assert call_kwargs["owner_id"] == "snapshot-owner"
    assert call_kwargs["page_size"] == 2
    assert call_kwargs["include_archived"] is False
    out = json.loads(capsys.readouterr().out)
    assert out["entity_count"] == 3
    assert [e["id"] for e in out["entities"]] == ["e1", "e2", "e3"]
    assert out["has_more"] is False
    assert out["next_cursor"] is None


def test_cli_export_writes_to_out_file_when_given(tmp_path, capsys):
    pages = [_page([{"id": "e1"}], entity_count=1, has_more=False, next_cursor=None)]
    out_path = tmp_path / "nested" / "snapshot.json"
    args = _parse(["export-corpus-snapshot", "--out", str(out_path)])
    with patch(
        "saltmdb.domain.services.corpus_snapshot_service.iter_corpus_snapshot_pages",
        return_value=iter(pages),
    ):
        rc = cli.cmd_export_corpus_snapshot(args)
    assert rc == 0
    assert capsys.readouterr().out == ""  # nothing printed when --out is given
    written = json.loads(out_path.read_text())
    assert written["entity_count"] == 1
    assert written["entities"] == [{"id": "e1"}]


def test_cli_export_reports_snapshot_changed_error(capsys):
    args = _parse(["export-corpus-snapshot"])
    with patch(
        "saltmdb.domain.services.corpus_snapshot_service.iter_corpus_snapshot_pages",
        side_effect=SnapshotChangedError("corpus changed mid-export"),
    ):
        rc = cli.cmd_export_corpus_snapshot(args)
    assert rc == 1
    assert "corpus changed mid-export" in capsys.readouterr().err


def test_cli_export_detects_entity_count_mismatch(capsys):
    # A page whose reported entity_count disagrees with what was actually merged must fail
    # closed rather than silently emitting an inconsistent snapshot.
    pages = [_page([{"id": "e1"}], entity_count=99, has_more=False, next_cursor=None)]
    args = _parse(["export-corpus-snapshot"])
    with patch(
        "saltmdb.domain.services.corpus_snapshot_service.iter_corpus_snapshot_pages",
        return_value=iter(pages),
    ):
        rc = cli.cmd_export_corpus_snapshot(args)
    assert rc == 1
    assert "entity_count" in capsys.readouterr().err
