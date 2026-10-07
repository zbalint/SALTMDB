"""Persistent daemon lifecycle and systemd user-unit management."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess  # nosec B404 -- systemd commands use fixed argument lists and never shell=True.
import sys
import time
from typing import Any, Callable, NamedTuple

from saltmdb import config
from saltmdb.daemon import client, discovery


_SYSTEMD_UNIT_NAME = "saltmdb-daemon.service"
_SYSTEMCTL_HINT = "On WSL2, enable systemd in /etc/wsl.conf ([boot] systemd=true) and restart WSL"
_PLATFORM_ERROR = "daemon commands support Linux and WSL2 only"
Runner = Callable[..., Any]


class ServiceResult(NamedTuple):
    """Structured result returned by every daemon lifecycle operation."""

    exit_code: int
    message: str


def _platform_guard() -> ServiceResult | None:
    if not sys.platform.startswith("linux"):
        return ServiceResult(1, _PLATFORM_ERROR)
    return None


def _canonical_db_path(db_path: str | None) -> str:
    return discovery.resolve_canonical_db_path(db_path)


def unit_path() -> str:
    """Return the systemd user-unit path for the current user."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config_home, "systemd", "user", _SYSTEMD_UNIT_NAME)


def _validate_unit_value(name: str, value: str) -> None:
    if any(character.isspace() or character in '%$"\\' for character in value):
        raise ValueError(f"{name} contains whitespace or a systemd-special character")


def render_unit(python: str, db_path: str) -> str:
    """Render the persistent daemon's systemd user unit."""
    _validate_unit_value("python", python)
    _validate_unit_value("db_path", db_path)
    return (
        "[Unit]\n"
        "Description=SALTMDB daemon (persistent)\n"
        "StartLimitIntervalSec=0\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"Environment=SALTMDB_DB_PATH={db_path}\n"
        f"ExecStart={python} -m saltmdb.daemon.server --foreground\n"
        "Restart=always\n"
        "RestartSec=30\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _runner_result(runner: Runner, command: list[str]) -> Any:
    try:
        return runner(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        return type("RunnerFailure", (), {"returncode": 127, "stderr": str(exc)})()


def _systemd_failure(action: str, result: Any) -> ServiceResult:
    stderr = str(getattr(result, "stderr", "") or "").strip()
    suffix = f": {stderr}" if stderr else ""
    return ServiceResult(1, f"{action} failed{suffix}; {_SYSTEMCTL_HINT}")


def _systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def install_service(
    db_path: str | None = None,
    *,
    dry_run: bool = False,
    no_start: bool = False,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    """Install or update the persistent daemon's systemd user unit."""
    unsupported = _platform_guard()
    if unsupported is not None:
        return unsupported
    if not _systemctl_available():
        return ServiceResult(1, "systemctl is required to install the daemon service")

    canonical_db_path = _canonical_db_path(db_path)
    unit = render_unit(sys.executable, canonical_db_path)
    path = unit_path()
    start_command = ["systemctl", "--user", "enable", _SYSTEMD_UNIT_NAME]
    if not no_start:
        start_command = ["systemctl", "--user", "enable", "--now", _SYSTEMD_UNIT_NAME]
    commands = [["systemctl", "--user", "daemon-reload"], start_command]

    if dry_run:
        rendered_commands = "\n".join(f"  {command}" for command in commands)
        return ServiceResult(
            0,
            f"dry-run: {path}\n{unit}\nplanned commands:\n{rendered_commands}",
        )

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        previous = None
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as unit_file:
                previous = unit_file.read()
        changed = (
            "unchanged"
            if previous == unit
            else ("updated" if previous is not None else "installed")
        )
        if previous != unit:
            with open(path, "w", encoding="utf-8") as unit_file:
                unit_file.write(unit)
        os.chmod(path, 0o644)
    except OSError as exc:
        return ServiceResult(1, f"cannot write {path}: {exc}")

    for command in commands:
        result = _runner_result(runner, command)
        if getattr(result, "returncode", 1) != 0:
            return _systemd_failure("systemd command", result)

    return ServiceResult(
        0,
        f"{changed}: {path}\n"
        "To start at boot without a login session: loginctl enable-linger $USER\n"
        "enable --now returning 0 does not mean the unit's daemon took over: run "
        "saltmdb-cli daemon status; if a daemon spawned by an agent holds the election, the "
        "unit's daemon exits as a loser and systemd retries every 30 s",
    )


def uninstall_service(*, runner: Runner = subprocess.run) -> ServiceResult:  # noqa: PLR0911 -- each lifecycle refusal is reported directly
    """Disable and remove the persistent daemon's systemd user unit."""
    unsupported = _platform_guard()
    if unsupported is not None:
        return unsupported
    path = unit_path()
    if not os.path.exists(path):
        return ServiceResult(0, "not installed")
    if not _systemctl_available():
        return ServiceResult(1, "systemctl is required to uninstall the daemon service")

    disable = _runner_result(
        runner,
        ["systemctl", "--user", "disable", "--now", _SYSTEMD_UNIT_NAME],
    )
    if getattr(disable, "returncode", 1) != 0:
        return _systemd_failure("systemd disable", disable)
    try:
        os.remove(path)
    except OSError as exc:
        return ServiceResult(1, f"cannot remove {path}: {exc}")
    reload_result = _runner_result(runner, ["systemctl", "--user", "daemon-reload"])
    if getattr(reload_result, "returncode", 1) != 0:
        return _systemd_failure("systemd daemon-reload", reload_result)
    return ServiceResult(0, "uninstalled")


def _read_proc_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as proc_file:
            return proc_file.read().replace(b"\x00", b" ").decode(errors="replace")
    except OSError:
        return ""


def _signal_local_daemon(pid: Any) -> str | None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return f"refusing to signal daemon pid {pid!r}: pid is not a positive integer"
    if "saltmdb.daemon.server" not in _read_proc_cmdline(pid):
        return f"refusing to signal pid {pid}: process is not saltmdb.daemon.server"
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return f"refusing to signal daemon pid {pid}: {exc}"
    return None


def _systemd_unit_state(runner: Runner) -> ServiceResult | None:
    result = _runner_result(
        runner,
        ["systemctl", "--user", "is-active", "--quiet", _SYSTEMD_UNIT_NAME],
    )
    code = getattr(result, "returncode", 1)
    if code == 0:
        return ServiceResult(1, "managed by systemd: use systemctl --user stop saltmdb-daemon")
    if code in (3, 4):
        return None
    return ServiceResult(
        1,
        "cannot determine whether the systemd unit is active "
        f"(systemctl exit {code}); stop it with systemctl --user stop saltmdb-daemon or fix the user session",
    )


def start_daemon(db_path: str | None = None) -> ServiceResult:
    """Start a persistent daemon after waiting for any existing owner."""
    unsupported = _platform_guard()
    if unsupported is not None:
        return unsupported
    canonical_db_path = _canonical_db_path(db_path)
    info = client.reachable_daemon_info(canonical_db_path)
    if info is not None:
        return ServiceResult(
            0,
            "already running: "
            f"pid={info.get('daemon_pid')} port={info.get('service_port')} "
            "(a daemon spawned by an agent exits 30 s after its last session; run stop, then "
            "start, for a persistent one)",
        )

    started_at = time.monotonic()
    owner = client.probe_owner(canonical_db_path)
    owner_misses = 0 if owner is not None else 1
    last_probe_at = started_at
    last_spawn_at: float | None = None
    spawned = False
    deadline = started_at + config.DAEMON_START_WAIT_S

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            tail = client._read_daemon_log_tail(canonical_db_path)
            return ServiceResult(
                1,
                f"timed out waiting for daemon after {config.DAEMON_START_WAIT_S:g} s; "
                f"see daemon.log\n{tail}",
            )
        time.sleep(min(config.DAEMON_DISCOVERY_RETRY_DELAY_S, remaining))
        info = client.reachable_daemon_info(canonical_db_path)
        if info is not None:
            if spawned:
                return ServiceResult(
                    0, f"started: pid={info.get('daemon_pid')} port={info.get('service_port')}"
                )
            return ServiceResult(
                0,
                "already running: "
                f"pid={info.get('daemon_pid')} port={info.get('service_port')} "
                "(a daemon spawned by an agent exits 30 s after its last session; run stop, then "
                "start, for a persistent one)",
            )

        now = time.monotonic()
        if now - last_probe_at >= config.DAEMON_OWNER_PROBE_INTERVAL_S:
            owner = client.probe_owner(canonical_db_path)
            last_probe_at = now
            if owner is None:
                owner_misses += 1
            else:
                owner_misses = 0

        if owner_misses < config.DAEMON_OWNER_PROBE_MISSES:
            continue
        if last_spawn_at is not None and now - last_spawn_at < config.DAEMON_SPAWN_MIN_INTERVAL_S:
            continue
        try:
            # shortcut: private client seams (_spawn_daemon_process here, _read_daemon_log_tail
            # above); make them public if a third caller appears.
            client._spawn_daemon_process(canonical_db_path, persistent=True)
        except OSError as exc:
            return ServiceResult(1, f"failed to spawn persistent daemon: {exc}; see daemon.log")
        spawned = True
        last_spawn_at = now
        owner_misses = 0


def stop_daemon(  # noqa: PLR0911 -- each lifecycle refusal is reported directly
    db_path: str | None = None,
    *,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    """Stop a local daemon after refusing active systemd-managed units."""
    unsupported = _platform_guard()
    if unsupported is not None:
        return unsupported
    canonical_db_path = _canonical_db_path(db_path)
    if os.path.exists(unit_path()):
        unit_state = _systemd_unit_state(runner)
        if unit_state is not None:
            return unit_state

    info = client.reachable_daemon_info(canonical_db_path)
    if info is None:
        if client.probe_owner(canonical_db_path) is not None:
            return ServiceResult(1, "the daemon is starting or stopping; try again shortly")
        return ServiceResult(0, "not running")

    pid = info.get("daemon_pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        refusal = _signal_local_daemon(pid)
        return ServiceResult(1, refusal or f"refusing to signal daemon pid {pid!r}")
    refusal = _signal_local_daemon(pid)
    if refusal is not None:
        return ServiceResult(1, refusal)

    deadline = time.monotonic() + config.DAEMON_STOP_WAIT_S
    while True:
        if "saltmdb.daemon.server" not in _read_proc_cmdline(pid):
            return ServiceResult(0, "stopped")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ServiceResult(
                1,
                f"daemon pid {pid} did not exit within {config.DAEMON_STOP_WAIT_S:g} s; "
                "it may still be draining, see daemon.log",
            )
        time.sleep(min(config.DAEMON_DISCOVERY_RETRY_DELAY_S, remaining))


def daemon_status(db_path: str | None = None) -> ServiceResult:
    """Report daemon reachability without exposing its authentication token."""
    unsupported = _platform_guard()
    if unsupported is not None:
        return unsupported
    canonical_db_path = _canonical_db_path(db_path)
    info = client.reachable_daemon_info(canonical_db_path)
    if info is not None:
        return ServiceResult(
            0,
            f"running pid={info.get('daemon_pid')} port={info.get('service_port')} "
            f"started_at={info.get('started_at')} db={canonical_db_path}",
        )
    if client.probe_owner(canonical_db_path) is not None:
        return ServiceResult(3, "starting or stopping")
    return ServiceResult(3, "not running")
