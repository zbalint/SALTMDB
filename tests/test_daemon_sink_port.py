"""Discovery ports reach every daemon connection sink as plain integers."""

from unittest.mock import MagicMock, patch

from saltmdb.daemon import client


class _Port(int):
    pass


def test_authenticated_ping_uses_plain_int_port():
    info = {"service_port": _Port(4000), "auth_token": "test-token"}
    with (
        patch.object(client.socket, "create_connection") as connect,
        patch.object(client.protocol, "send_frame"),
        patch.object(client.protocol, "recv_frame", return_value={"ok": True}),
    ):
        assert client._authenticated_ping_ok(info) is True

    connect.assert_called_once()
    assert connect.call_args.args[0] == ("127.0.0.1", 4000)
    assert type(connect.call_args.args[0][1]) is int


def test_call_request_uses_plain_int_port():
    info = {"service_port": _Port(4000), "auth_token": "test-token"}
    with (
        patch.object(client.socket, "create_connection") as connect,
        patch.object(client.protocol, "send_frame"),
        patch.object(client.protocol, "recv_frame", return_value={"ok": True}),
    ):
        assert client._send_call_request(info, "ping", {}) == {"ok": True}

    connect.assert_called_once()
    assert connect.call_args.args[0] == ("127.0.0.1", 4000)
    assert type(connect.call_args.args[0][1]) is int


def test_session_open_uses_plain_int_port(tmp_path):
    info = {"service_port": _Port(4000), "auth_token": "test-token"}
    session = client.SessionConnection(str(tmp_path / "sink-port.db"))
    sock = MagicMock()
    with (
        patch.object(client, "_current_session", None),
        patch.object(client, "ensure_daemon_running", return_value=info),
        patch.object(client.socket, "create_connection", return_value=sock) as connect,
        patch.object(client.protocol, "send_frame"),
        patch.object(client.protocol, "recv_frame", return_value={"ok": True}),
    ):
        try:
            session.open()
        finally:
            session.close(send_goodbye=False)

    connect.assert_called_once()
    assert connect.call_args.args[0] == ("127.0.0.1", 4000)
    assert type(connect.call_args.args[0][1]) is int
