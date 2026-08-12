"""MCP/daemon boundary tests for the read-only corpus snapshot export."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from saltmdb.daemon.dispatch import _dispatch_export_corpus_snapshot
from saltmdb.mcp import tools


def test_dispatch_snapshot_applies_read_only_defaults_and_forwards_cursor():
    with patch(
        "saltmdb.daemon.dispatch.corpus_snapshot_service.export_corpus_snapshot_page",
        return_value={"snapshot_hash": "a" * 64},
    ) as export:
        result = _dispatch_export_corpus_snapshot(
            owner_id="snapshot-owner",
            page_size=7,
            cursor="entity-006",
            snapshot_hash="a" * 64,
            include_archived=True,
        )

    assert result == {"snapshot_hash": "a" * 64}
    export.assert_called_once_with(
        owner_id="snapshot-owner",
        page_size=7,
        cursor="entity-006",
        snapshot_hash="a" * 64,
        include_archived=True,
    )


def test_mcp_snapshot_wrapper_resolves_aliases_without_sql_access():
    backend = MagicMock()
    backend.call.return_value = {"entities": [], "snapshot_hash": "b" * 64}
    previous = tools._set_backend_for_test(backend)
    try:
        result = tools.export_corpus_snapshot(
            owner_id="snapshot-owner",
            limit=4,
            after_id="entity-003",
            snapshot_id="b" * 64,
            include_archived_entities=True,
        )
    finally:
        tools._set_backend_for_test(previous)

    assert result == {"entities": [], "snapshot_hash": "b" * 64}
    backend.call.assert_called_once_with(
        "export_corpus_snapshot",
        {
            "owner_id": "snapshot-owner",
            "page_size": 4,
            "cursor": "entity-003",
            "snapshot_hash": "b" * 64,
            "include_archived": True,
        },
    )


def test_dispatch_snapshot_requires_owner_id():
    with pytest.raises(ValueError, match="owner_id is required"):
        _dispatch_export_corpus_snapshot(page_size=2)


def test_mcp_snapshot_wrapper_requires_owner_id():
    previous = tools._set_backend_for_test(MagicMock())
    try:
        with pytest.raises(ValueError, match="owner_id is mandatory"):
            tools.export_corpus_snapshot(owner_id=None, page_size=2)
    finally:
        tools._set_backend_for_test(previous)
