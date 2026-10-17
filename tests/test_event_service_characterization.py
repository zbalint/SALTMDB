import json

import pytest

from saltmdb.db.schema import init_db
from saltmdb.domain.services.event_service import dismiss_events, get_recent_events


@pytest.fixture
def event_db(tmp_path):
    path = str(tmp_path / "events.db")
    conn = init_db(path)
    conn.executemany(
        "INSERT INTO events (id, timestamp, agent_id, type, content, agent_session_id, context_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("a", "2026-01-01", "agent-a", "issue", "x" * 1001, "session-a", "context-a"),
            ("b", "2026-01-02", "agent-b", "decision", "y" * 1000, "session-b", "context-b"),
            (
                "c",
                "2026-01-03",
                "agent-a",
                "consolidation_request",
                "review",
                "session-a",
                "context-a",
            ),
            (
                "d",
                "2026-01-04",
                "agent-a",
                "supersession_candidate",
                "review",
                "session-a",
                "context-a",
            ),
        ],
    )
    conn.commit()
    yield conn, path
    conn.close()


def test_recent_events_filters_order_pagination_and_content_boundary(event_db):
    conn, path = event_db
    rows = get_recent_events(db_path=path, order="oldest_first", limit=2)
    assert [row["id"] for row in rows] == ["a", "b"]
    assert rows[0] == {
        "id": "a",
        "timestamp": "2026-01-01",
        "agent_id": "agent-a",
        "type": "issue",
        "content": "x" * 1000 + " [TRUNCATED]",
        "error_code": None,
        "agent_session_id": "session-a",
        "context_id": "context-a",
    }
    assert rows[1]["content"] == "y" * 1000
    assert [row["id"] for row in get_recent_events(db_connection=conn, offset=1, limit=2)] == [
        "c",
        "b",
    ]
    assert [
        row["id"]
        for row in get_recent_events(
            context_id="context-a",
            agent_id="agent-a",
            type_filter="issue",
            agent_session_id="session-a",
            db_connection=conn,
        )
    ] == ["a"]
    assert get_recent_events(event_id="a", db_connection=conn)[0]["content"] == "x" * 1001
    assert get_recent_events(full_content=True, db_connection=conn)[-1]["content"] == "x" * 1001
    assert get_recent_events(agent_id="missing", db_connection=conn) == []


def test_recent_events_read_failure_returns_error(event_db):
    conn, _ = event_db
    conn.execute("DROP TABLE events")
    assert get_recent_events(db_connection=conn) == [{"error": "no such table: events"}]


def test_dismissal_deduplicates_and_is_idempotent(event_db):
    conn, path = event_db
    assert (
        dismiss_events(["c", "c", "d"], "reviewed", agent_id="reviewer", db_path=path)
        == "Events dismissed successfully"
    )
    assert dismiss_events("c", "again", db_connection=conn) == "Events dismissed successfully"
    rows = get_recent_events(type_filter="event_dismissed", db_connection=conn)
    contents = sorted(
        (json.loads(row["content"]) for row in rows), key=lambda row: row["target_event_id"]
    )
    assert contents == [
        {
            "target_event_id": "c",
            "reason": "reviewed",
            "target_type": "consolidation_request",
            "dismissed_by": "reviewer",
        },
        {
            "target_event_id": "d",
            "reason": "reviewed",
            "target_type": "supersession_candidate",
            "dismissed_by": "reviewer",
        },
    ]


@pytest.mark.parametrize("reason", ["", " \t"])
def test_dismissal_rejects_blank_reason(event_db, reason):
    conn, _ = event_db
    with pytest.raises(ValueError, match="^Dismissal reason cannot be empty$"):
        dismiss_events("c", reason, db_connection=conn)


@pytest.mark.parametrize(
    "ids,message",
    [
        (["c", "absent"], "Events not found: ['absent']"),
        (["c", "a"], "Events are not dismissible types: ['a']"),
    ],
)
def test_invalid_dismissal_does_not_partially_write(event_db, ids, message):
    conn, _ = event_db
    with pytest.raises(ValueError) as exc:
        dismiss_events(ids, "reviewed", db_connection=conn)
    assert str(exc.value) == message
    assert get_recent_events(type_filter="event_dismissed", db_connection=conn) == []


def test_dismissal_uses_ambient_transaction_and_rolls_back_with_caller(event_db):
    conn, _ = event_db
    conn.execute("BEGIN IMMEDIATE")
    dismiss_events("c", "reviewed", db_connection=conn, _in_transaction=True)
    assert [
        json.loads(row["content"])["target_event_id"]
        for row in get_recent_events(
            type_filter="event_dismissed",
            db_connection=conn,
        )
    ] == ["c"]
    conn.rollback()
    assert get_recent_events(type_filter="event_dismissed", db_connection=conn) == []
    assert dismiss_events([], "reviewed", db_connection=conn) == "Events dismissed successfully"
