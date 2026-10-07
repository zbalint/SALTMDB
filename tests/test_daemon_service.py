import os
import signal
import tempfile
import time as real_time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from saltmdb.daemon import discovery, service
from saltmdb.config import (
    DAEMON_OWNER_PROBE_MISSES,
    DAEMON_SPAWN_MIN_INTERVAL_S,
)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _Runner:
    def __init__(self, returncodes=None, stderr=""):
        self.calls = []
        self.returncodes = list(returncodes or [])
        self.stderr = stderr

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        code = self.returncodes.pop(0) if self.returncodes else 0
        return SimpleNamespace(returncode=code, stderr=self.stderr)


class TestDaemonService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "saltmdb.db")
        self.canonical_db_path = discovery.resolve_canonical_db_path(self.db_path)
        self.info = {
            "db_path": self.canonical_db_path,
            "daemon_pid": 1234,
            "service_port": 5678,
            "started_at": "2026-10-07T00:00:00+00:00",
            "auth_token": "secret-token",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_render_unit_exact_text(self):
        self.assertEqual(
            service.render_unit("/home/u/.venv/bin/python", "/home/u/.saltmdb/saltmdb.db"),
            "[Unit]\n"
            "Description=SALTMDB daemon (persistent)\n"
            "StartLimitIntervalSec=0\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            "Environment=SALTMDB_DB_PATH=/home/u/.saltmdb/saltmdb.db\n"
            "ExecStart=/home/u/.venv/bin/python -m saltmdb.daemon.server --foreground\n"
            "Restart=always\n"
            "RestartSec=30\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n",
        )

    def test_render_unit_rejects_unsafe_values(self):
        cases = [
            ("python path with space", "python", "/home/u/.venv/bin/my python"),
            ("db path with percent", "db_path", "/home/u/100%.db"),
            ("value with newline", "db_path", "/home/u/line\nbreak.db"),
        ]
        for label, value_name, value in cases:
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, value_name):
                service.render_unit(value if value_name == "python" else "/python", value)

    def test_install_service_writes_unit_and_runs_commands(self):
        runner = _Runner()
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temp_dir.name}),
            patch.object(service.shutil, "which", return_value="/usr/bin/systemctl"),
        ):
            result = service.install_service(self.db_path, runner=runner)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("loginctl enable-linger", result.message)
        self.assertIn("daemon status", result.message)
        path = Path(self.temp_dir.name) / "systemd" / "user" / "saltmdb-daemon.service"
        self.assertEqual(
            path.read_text(), service.render_unit(service.sys.executable, self.canonical_db_path)
        )
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", "saltmdb-daemon.service"],
            ],
        )

        runner = _Runner()
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temp_dir.name}),
            patch.object(service.shutil, "which", return_value="/usr/bin/systemctl"),
        ):
            result = service.install_service(self.db_path, no_start=True, runner=runner)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(
            [call[0] for call in runner.calls][-1],
            ["systemctl", "--user", "enable", "saltmdb-daemon.service"],
        )

    def test_install_service_is_idempotent_and_updates(self):
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temp_dir.name}),
            patch.object(service.shutil, "which", return_value="/usr/bin/systemctl"),
        ):
            first = service.install_service(self.db_path, runner=_Runner())
            same = service.install_service(self.db_path, runner=_Runner())
            changed_path = os.path.join(self.temp_dir.name, "other.db")
            changed = service.install_service(changed_path, runner=_Runner())
        self.assertIn("installed", first.message)
        self.assertIn("unchanged", same.message)
        self.assertIn("updated", changed.message)
        unit = Path(self.temp_dir.name) / "systemd" / "user" / "saltmdb-daemon.service"
        self.assertIn(os.path.realpath(changed_path), unit.read_text())

    def test_install_service_dry_run_does_not_write_or_run(self):
        runner = _Runner()
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temp_dir.name}),
            patch.object(service.shutil, "which", return_value="/usr/bin/systemctl"),
        ):
            result = service.install_service(self.db_path, dry_run=True, runner=runner)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("ExecStart=", result.message)
        self.assertEqual(runner.calls, [])
        self.assertFalse(
            (Path(self.temp_dir.name) / "systemd" / "user" / "saltmdb-daemon.service").exists()
        )

    def test_install_service_reports_systemd_failure(self):
        runner = _Runner([1], stderr="systemd unavailable")
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temp_dir.name}),
            patch.object(service.shutil, "which", return_value="/usr/bin/systemctl"),
        ):
            result = service.install_service(self.db_path, runner=runner)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("systemd unavailable", result.message)
        self.assertIn("systemd=true", result.message)

    def test_platform_guard_and_missing_systemctl(self):
        runner = _Runner()
        with patch.object(service.sys, "platform", "darwin"):
            for function, kwargs in (
                (service.install_service, {"runner": runner}),
                (service.uninstall_service, {"runner": runner}),
                (service.start_daemon, {}),
                (service.stop_daemon, {"runner": runner}),
                (service.daemon_status, {}),
            ):
                result = (
                    function(self.db_path, **kwargs)
                    if function not in {service.uninstall_service}
                    else function(**kwargs)
                )
                self.assertEqual(result.exit_code, 1)
                self.assertIn("Linux and WSL2 only", result.message)
        with (
            patch.object(service.sys, "platform", "linux"),
            patch.object(service.shutil, "which", return_value=None),
        ):
            result = service.install_service(self.db_path, runner=runner)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("systemctl", result.message)
        self.assertEqual(runner.calls, [])

    def test_uninstall_service_missing_and_installed(self):
        runner = _Runner()
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temp_dir.name}):
            result = service.uninstall_service(runner=runner)
        self.assertEqual(result, service.ServiceResult(0, "not installed"))
        self.assertEqual(runner.calls, [])

        unit = Path(self.temp_dir.name) / "systemd" / "user" / "saltmdb-daemon.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("unit")
        runner = _Runner()
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temp_dir.name}),
            patch.object(service.shutil, "which", return_value="/usr/bin/systemctl"),
        ):
            result = service.uninstall_service(runner=runner)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(unit.exists())
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ["systemctl", "--user", "disable", "--now", "saltmdb-daemon.service"],
                ["systemctl", "--user", "daemon-reload"],
            ],
        )

    def test_stop_refuses_active_systemd_unit_and_handles_unknown_state(self):
        unit = Path(self.temp_dir.name) / "systemd" / "user" / "saltmdb-daemon.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("unit")
        for code in (0, 1, 3, 4):
            runner = _Runner([code])
            with (
                patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temp_dir.name}),
                patch.object(service.shutil, "which", return_value="/usr/bin/systemctl"),
                patch.object(service.client, "reachable_daemon_info", return_value=self.info),
                patch.object(
                    service, "_read_proc_cmdline", side_effect=["saltmdb.daemon.server", ""]
                ),
                patch.object(service.os, "kill") as kill,
            ):
                result = service.stop_daemon(self.db_path, runner=runner)
            if code == 0:
                self.assertEqual(result.exit_code, 1)
                self.assertIn("managed by systemd", result.message)
                kill.assert_not_called()
            elif code == 1:
                self.assertEqual(result.exit_code, 1)
                self.assertIn("cannot determine", result.message)
                kill.assert_not_called()
            else:
                self.assertEqual(result.exit_code, 0)
                kill.assert_called_once_with(self.info["daemon_pid"], signal.SIGTERM)

    def test_stop_refuses_non_daemon_or_invalid_pid(self):
        for info in ({**self.info, "daemon_pid": 0}, {**self.info, "daemon_pid": "1234"}):
            with patch.object(service.client, "reachable_daemon_info", return_value=info):
                result = service.stop_daemon(self.db_path)
            self.assertEqual(result.exit_code, 1)
            self.assertIn("refus", result.message)
        with (
            patch.object(service.client, "reachable_daemon_info", return_value=self.info),
            patch.object(service, "_read_proc_cmdline", return_value="python other.py"),
            patch.object(service.os, "kill") as kill,
        ):
            result = service.stop_daemon(self.db_path)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("saltmdb.daemon.server", result.message)
        kill.assert_not_called()

    def test_stop_waits_for_process_exit(self):
        clock = _Clock()
        proc_states = iter(["saltmdb.daemon.server"] * 21 + [""])
        with (
            patch.object(service, "time", clock),
            patch.object(service.client, "reachable_daemon_info", side_effect=[self.info, None]),
            patch.object(service, "_read_proc_cmdline", side_effect=lambda _pid: next(proc_states)),
            patch.object(service.os, "kill") as kill,
        ):
            result = service.stop_daemon(self.db_path)
        self.assertEqual(result, service.ServiceResult(0, "stopped"))
        kill.assert_called_once_with(1234, signal.SIGTERM)
        self.assertGreaterEqual(clock.now, 5.0)

    def test_stop_timeout_never_sends_sigkill(self):
        clock = _Clock()
        with (
            patch.object(service, "time", clock),
            patch.object(service.client, "reachable_daemon_info", return_value=self.info),
            patch.object(service, "_read_proc_cmdline", return_value="saltmdb.daemon.server"),
            patch.object(service.os, "kill") as kill,
        ):
            result = service.stop_daemon(self.db_path)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("1234", result.message)
        self.assertIn("daemon.log", result.message)
        kill.assert_called_once_with(1234, signal.SIGTERM)
        self.assertFalse(any(call.args[1] == signal.SIGKILL for call in kill.call_args_list))

    def test_status_and_stop_owner_states(self):
        with patch.object(service.client, "reachable_daemon_info", return_value=self.info):
            result = service.daemon_status(self.db_path)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("pid=1234", result.message)
        self.assertIn("port=5678", result.message)
        self.assertNotIn("secret-token", result.message)

        for owner in ("initializing", "ready"):
            with (
                patch.object(service.client, "reachable_daemon_info", return_value=None),
                patch.object(service.client, "probe_owner", return_value=owner),
            ):
                status = service.daemon_status(self.db_path)
                stopped = service.stop_daemon(self.db_path)
            self.assertEqual(status, service.ServiceResult(3, "starting or stopping"))
            self.assertEqual(stopped.exit_code, 1)
            self.assertIn("starting or stopping", stopped.message)
        with (
            patch.object(service.client, "reachable_daemon_info", return_value=None),
            patch.object(service.client, "probe_owner", return_value=None),
        ):
            result = service.daemon_status(self.db_path)
        self.assertEqual(result, service.ServiceResult(3, "not running"))

    def test_start_existing_daemon_does_not_spawn(self):
        with (
            patch.object(service.client, "reachable_daemon_info", return_value=self.info),
            patch.object(service.client, "_spawn_daemon_process") as spawn,
        ):
            result = service.start_daemon(self.db_path)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("already running", result.message)
        self.assertIn("stop, then start", result.message)
        spawn.assert_not_called()

    def test_start_spawns_after_owner_misses_and_waits_for_initializing_owner(self):
        clock = _Clock()
        spawn = []

        def reachable(_path):
            return self.info if clock.now >= 10 else None

        with (
            patch.object(service, "time", clock),
            patch.object(service.client, "reachable_daemon_info", side_effect=reachable),
            patch.object(service.client, "probe_owner", return_value=None) as probe,
            patch.object(
                service.client,
                "_spawn_daemon_process",
                side_effect=lambda *args, **kwargs: spawn.append((clock.now, kwargs)),
            ),
        ):
            result = service.start_daemon(self.db_path)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("started", result.message)
        self.assertEqual(len(spawn), 1)
        self.assertTrue(spawn[0][1]["persistent"])
        self.assertGreaterEqual(probe.call_count, DAEMON_OWNER_PROBE_MISSES)
        self.assertLessEqual(probe.call_count, 11)

        clock = _Clock()
        spawn = []

        def initializing_reachable(_path):
            return self.info if clock.now >= 20 else None

        with (
            patch.object(service, "time", clock),
            patch.object(
                service.client, "reachable_daemon_info", side_effect=initializing_reachable
            ),
            patch.object(service.client, "probe_owner", return_value="initializing"),
            patch.object(
                service.client,
                "_spawn_daemon_process",
                side_effect=lambda *args, **kwargs: spawn.append(clock.now),
            ),
        ):
            result = service.start_daemon(self.db_path)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(spawn, [])

    def test_start_timeout_reads_log_and_throttles_spawns(self):
        clock = _Clock()
        spawns = []
        with (
            patch.object(service, "time", clock),
            patch.object(service.client, "reachable_daemon_info", return_value=None),
            patch.object(service.client, "probe_owner", return_value=None),
            patch.object(
                service.client,
                "_spawn_daemon_process",
                side_effect=lambda *args, **kwargs: spawns.append(clock.now),
            ),
            patch.object(
                service.client, "_read_daemon_log_tail", return_value="last log lines"
            ) as tail,
        ):
            result = service.start_daemon(self.db_path)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("daemon.log", result.message)
        self.assertIn("last log lines", result.message)
        tail.assert_called_once_with(self.canonical_db_path)
        self.assertGreaterEqual(len(spawns), 2)
        self.assertTrue(
            all(
                later - earlier >= DAEMON_SPAWN_MIN_INTERVAL_S
                for earlier, later in zip(spawns, spawns[1:])
            )
        )

    def test_real_persistent_daemon_lifecycle(self):
        env = {"SALTMDB_VIEWER_ENABLED": "false"}
        canonical = discovery.resolve_canonical_db_path(self.db_path)
        key = discovery.daemon_key(canonical)
        try:
            with patch.dict(os.environ, env, clear=False):
                started = service.start_daemon(self.db_path)
                self.assertEqual(started.exit_code, 0)
                already = service.start_daemon(self.db_path)
                self.assertEqual(already.exit_code, 0)
                self.assertIn("already running", already.message)
                status = service.daemon_status(self.db_path)
                self.assertEqual(status.exit_code, 0)
                self.assertNotIn(discovery.read(key)["auth_token"], status.message)
                stopped = service.stop_daemon(self.db_path)
                self.assertEqual(stopped, service.ServiceResult(0, "stopped"))
                self.assertEqual(service.daemon_status(self.db_path).exit_code, 3)
                self.assertEqual(
                    service.stop_daemon(self.db_path), service.ServiceResult(0, "not running")
                )
        finally:
            info = discovery.read(key)
            if info:
                pid = info.get("daemon_pid")
                if (
                    isinstance(pid, int)
                    and pid > 0
                    and "saltmdb.daemon.server" in service._read_proc_cmdline(pid)
                ):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
                    deadline = real_time.time() + 15
                    while real_time.time() < deadline and service._read_proc_cmdline(pid):
                        real_time.sleep(0.1)
            discovery.remove(key)


if __name__ == "__main__":
    unittest.main()
