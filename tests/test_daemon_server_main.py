"""Characterize the real daemon entry point without using the working daemon."""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch


import pytest

from saltmdb.daemon import discovery, protocol, server


@contextmanager
def running_main(tmp_path, **overrides):
    db_path = str(overrides.pop("SALTMDB_DB_PATH", tmp_path / "main.db"))
    home = Path(overrides.pop("HOME", tmp_path))
    log_path = Path(overrides.pop("LOG_PATH", tmp_path / "main.log"))
    key = discovery.daemon_key(db_path)
    discovery_file = home / ".saltmdb" / f"daemon_{key}.json"
    env = dict(os.environ)
    env.pop("SALTMDB_DAEMON_SPAWNED_AT", None)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "SALTMDB_DB_PATH": db_path,
            "SALTMDB_VIEWER_ENABLED": "false",
            "SALTMDB_ENABLE_SEMANTIC": "false",
            **overrides,
        }
    )
    with log_path.open("w+") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "saltmdb.daemon.server", "--foreground"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            yield proc, discovery_file, log_path
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def wait_until_ready(proc, discovery_file, log_path):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        output = log_path.read_text()
        if discovery_file.exists() and "Signal handlers registered:" in output:
            return json.loads(discovery_file.read_text())
        assert proc.poll() is None, output
        time.sleep(0.05)
    pytest.fail(f"daemon did not become ready:\n{log_path.read_text()}")


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX signal delivery")
@pytest.mark.parametrize(
    ("shutdown_signal", "spawned_at"),
    [(signal.SIGTERM, None), (signal.SIGINT, "invalid"), (signal.SIGTERM, "0")],
)
def test_main_rpc_discovery_and_ordered_signal_shutdown(tmp_path, shutdown_signal, spawned_at):
    overrides = {} if spawned_at is None else {"SALTMDB_DAEMON_SPAWNED_AT": spawned_at}
    with running_main(tmp_path, **overrides) as (proc, discovery_file, log_path):
        info = wait_until_ready(proc, discovery_file, log_path)
        assert set(info) == {
            "schema_version",
            "db_path",
            "daemon_pid",
            "service_port",
            "auth_token",
            "started_at",
        }
        assert info["db_path"] == str(tmp_path / "main.db")
        assert info["daemon_pid"] == proc.pid
        assert discovery_file.stat().st_mode & 0o777 == 0o600
        with socket.create_connection(("127.0.0.1", info["service_port"]), timeout=5) as sock:
            request = protocol.build_request("ping", {}, token=info["auth_token"])
            protocol.send_frame(sock, request)
            assert protocol.recv_frame(sock) == {
                "id": request["id"],
                "ok": True,
                "result": {"status": "ok"},
            }
        proc.send_signal(shutdown_signal)
        assert proc.wait(timeout=20) == 0
        assert not discovery_file.exists()
        output = log_path.read_text()
        markers = [
            "SALTMDB daemon ready:",
            "Daemon startup phases:",
            "Signal handlers registered: SIGINT+SIGTERM",
            "Shutdown requested: trigger_args=",
            "Shutdown watcher woken; beginning shutdown sequence",
            "Shutdown sequence starting",
            "SALTMDB daemon shutdown complete.",
            "About to call os._exit(0) now",
        ]
        positions = [output.index(marker) for marker in markers]
        assert positions == sorted(positions)
        assert "serve_forever() returned; main thread blocking on watcher_thread.join()" in output
        assert output.count("Shutdown sequence starting") == 1
        if spawned_at != "0":
            assert "foreground=True startup_lag_s=n/a" in output
        else:
            assert "foreground=True startup_lag_s=n/a" not in output


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX signal delivery")
def test_main_viewer_status_reports_configured_port(tmp_path):
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        viewer_port = port_probe.getsockname()[1]
    with running_main(
        tmp_path,
        SALTMDB_VIEWER_ENABLED="true",
        SALTMDB_VIEWER_PORT=str(viewer_port),
    ) as (proc, discovery_file, log_path):
        info = wait_until_ready(proc, discovery_file, log_path)
        with socket.create_connection(("127.0.0.1", info["service_port"]), timeout=5) as sock:
            request = protocol.build_request("viewer_status", {}, token=info["auth_token"])
            protocol.send_frame(sock, request)
            response = protocol.recv_frame(sock)
        assert response["ok"] is True
        assert response["result"]["enabled"] is True
        assert response["result"]["port"] == viewer_port
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0
        assert not discovery_file.exists()


def test_main_starts_probe_thread_before_durable_services():
    order = []

    class FakeState:
        def __init__(self, *_args):
            self.service_port = None

        def _acquire_inflight(self):
            return False

        def set_shutdown_callback(self, _callback):
            pass

        def start_initial_grace_period(self):
            pass

    class FakeService:
        server_address = ("127.0.0.1", 45678)

        def serve_forever(self):
            raise KeyboardInterrupt

    class FakeMonitor:
        def start(self):
            pass

    def fake_probe_thread(_probe_sock, _state):
        order.append("probe")

    def fake_durable_services(_state, _db_path, _probe_sock, _guard):
        order.append("durable")
        return 2.0, 3.0

    def fake_service_server(state):
        state.service_port = 45678
        return FakeService(), 45678

    with (
        patch.object(server, "_parse_args", return_value=SimpleNamespace(foreground=True)),
        patch.object(server, "_prepare_startup", return_value=0.0),
        patch.object(server.platform_paths, "classify_db_path", return_value="local"),
        patch.object(server.discovery, "resolve_canonical_db_path", return_value="/tmp/test.db"),
        patch.object(server.discovery, "daemon_key", return_value="test-key"),
        patch.object(server.discovery, "election_port", return_value=12345),
        patch.object(server.discovery, "probe_port", return_value=12346),
        patch.object(server, "_bind_daemon_sockets", return_value=(object(), object(), 1.0)),
        patch.object(server, "_DaemonState", FakeState),
        patch.object(server, "_start_probe_thread", side_effect=fake_probe_thread),
        patch.object(server, "_start_durable_services", side_effect=fake_durable_services),
        patch.object(server, "_start_service_server", side_effect=fake_service_server),
        patch.object(server.config, "is_viewer_enabled", return_value=False),
        patch.object(server, "_publish_ready"),
        patch.object(server, "EmbedStallMonitor", return_value=FakeMonitor()),
        patch.object(server.signal, "signal"),
        patch.object(server, "_shutdown_sequence"),
    ):
        server.main()

    assert order == ["probe", "durable"]


@pytest.mark.parametrize("occupied", ["guard", "probe", "viewer"])
def test_main_occupied_ports_fail_without_discovery_or_retained_guard(tmp_path, occupied):
    key = discovery.daemon_key(str(tmp_path / "main.db"))
    guard_port = discovery.election_port(key)
    probe_port = discovery.probe_port(key)
    port = {"guard": guard_port, "probe": probe_port, "viewer": 0}[occupied]
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        overrides = {}
        if occupied == "viewer":
            overrides = {
                "SALTMDB_VIEWER_ENABLED": "true",
                "SALTMDB_VIEWER_PORT": str(blocker.getsockname()[1]),
            }
        with running_main(tmp_path, **overrides) as (proc, discovery_file, log_path):
            assert proc.wait(timeout=30) == 1
            assert not discovery_file.exists()
            assert "SALTMDB daemon ready:" not in log_path.read_text()
            if occupied == "probe":
                assert f"Failed to bind probe port {probe_port}:" in log_path.read_text()
            if occupied == "viewer":
                assert "Viewer bind failed on port" in log_path.read_text()
            if occupied != "guard":
                with socket.socket() as released_guard:
                    released_guard.bind(("127.0.0.1", guard_port))


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX signal delivery")
def test_main_existing_owner_loses_election_cleanly(tmp_path):
    with running_main(tmp_path) as (owner, discovery_file, log_path):
        info = wait_until_ready(owner, discovery_file, log_path)
        loser_dir = tmp_path / "loser"
        loser_dir.mkdir()
        with running_main(
            loser_dir,
            SALTMDB_DB_PATH=info["db_path"],
            HOME=str(tmp_path),
            LOG_PATH=loser_dir / "loser.log",
        ) as (loser, _, loser_log):
            assert loser.wait(timeout=15) == 0
            assert "SALTMDB daemon ready:" not in loser_log.read_text()
        assert json.loads(discovery_file.read_text()) == info
        owner.send_signal(signal.SIGTERM)
        assert owner.wait(timeout=20) == 0
