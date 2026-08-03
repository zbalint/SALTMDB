import sys
import os
import socket
import socketserver
import subprocess
import time
import logging
import threading
import urllib.request
from typing import Any
from saltmdb.config import get_db_path, VIEWER_SHIM_PATH
from saltmdb.db.connection import get_connection
from saltmdb.db.viewer_sessions import count_live_sessions
from saltmdb.viewer.routes import SALTMDBHandler

logger = logging.getLogger(__name__)


def _run_liveness_watchdog(
    httpd: socketserver.TCPServer,
    port: int,
    check_interval: float = 15.0,
    grace_period: float = 30.0,
) -> None:
    """Periodically checks active MCP sessions in _viewer_sessions table.

    If no live sessions exist after initial grace_period, shuts down the HTTP server.
    """
    time.sleep(grace_period)
    while True:
        try:
            db_path = get_db_path()
            if os.path.exists(db_path):
                conn = get_connection(db_path)
                try:
                    live_count = count_live_sessions(conn, port)
                finally:
                    conn.close()

                if live_count == 0:
                    logger.warning(
                        "No active MCP sessions found for viewer port %d. Shutting down viewer server.",
                        port,
                    )
                    httpd.shutdown()
                    break
        except Exception as e:
            logger.debug("Error in viewer liveness watchdog: %s", e)
        time.sleep(check_interval)


def start_viewer(port: int = 8080) -> str:  # noqa: C901, PLR0912, PLR0915
    """Spawns the local SALTMDB web dashboard/viewer in the background on specified port."""
    port = port or 8080

    is_running = False
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/", timeout=0.5) as res:
            if res.status == 200:
                is_running = True
    except Exception:
        pass

    if is_running:
        return f"SALTMDB Database Viewer is already running! Open it in your browser at http://localhost:{port}"

    for _ in range(10):
        port_occupied = False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.1)
            s.connect(("127.0.0.1", port))
            s.close()
            port_occupied = True
        except Exception:
            pass

        if not port_occupied:
            break
        stop_viewer(port=port)
        time.sleep(0.1)

    try:
        viewer_script = VIEWER_SHIM_PATH
        if not os.path.exists(viewer_script):
            viewer_cmd = [sys.executable, "-u", "-m", "saltmdb.viewer.server", "--port", str(port)]
        else:
            viewer_cmd = [sys.executable, "-u", viewer_script, "--port", str(port)]

        log_dir = os.path.expanduser("~/.saltmdb")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "viewer.log")

        with open(log_path, "w", encoding="utf-8") as f:
            f.write("")

        log_file = open(log_path, "a", encoding="utf-8")

        env = dict(os.environ)
        env["SALTMDB_DB_PATH"] = get_db_path()
        env["SALTMDB_VIEWER_PORT"] = str(port)

        popen_kwargs: dict[str, Any] = {"stdout": log_file, "stderr": log_file, "env": env}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        else:
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen(viewer_cmd, **popen_kwargs)
        log_file.close()

        # Store PID for clean shutdown
        try:
            pid_file = os.path.join(os.path.expanduser("~/.saltmdb"), f"viewer_{port}.pid")
            with open(pid_file, "w") as pf:
                pf.write(str(process.pid))
        except Exception:
            pass

        server_started = False
        for _ in range(30):
            if process.poll() is not None:
                break
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.1)
                s.connect(("127.0.0.1", port))
                s.close()
                server_started = True
                break
            except Exception:
                pass
            time.sleep(0.1)

        if not server_started:
            poll = process.poll()
            log_snippet = ""
            try:
                if os.path.exists(log_path):
                    with open(log_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                        log_snippet = "".join(lines[-15:])
            except Exception:
                pass
            exit_code_str = f"code {poll}" if poll is not None else "timeout"
            return f"Error: Database viewer failed to start: {exit_code_str}.\nLog snippet:\n{log_snippet}"

        return f"SALTMDB Database Viewer started successfully! Open it in your browser at http://localhost:{port}"
    except Exception as e:
        logger.error("Error starting database viewer: %s", e)
        return f"Error starting database viewer: {e}"


def stop_viewer(port: int = 8080) -> str:  # noqa: C901, PLR0912
    """Stops the running local SALTMDB web dashboard/viewer."""
    port = port or 8080

    # Try PID-based termination first (precise, no false positives)
    try:
        pid_file = os.path.join(os.path.expanduser("~/.saltmdb"), f"viewer_{port}.pid")
        if os.path.exists(pid_file):
            with open(pid_file) as pf:
                pid = int(pf.read().strip())

            is_saltmdb_viewer = False
            try:
                if sys.platform == "win32":
                    cmd = [
                        "powershell",
                        "-Command",
                        f'(Get-CimInstance Win32_Process -Filter "ProcessId={pid}").CommandLine',
                    ]
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                    if res.returncode == 0 and res.stdout and "saltmdb" in res.stdout.lower():
                        is_saltmdb_viewer = True
                else:
                    proc_cmdline = f"/proc/{pid}/cmdline"
                    if os.path.exists(proc_cmdline):
                        with open(proc_cmdline, "rb") as f:
                            cmdline_str = f.read().decode("utf-8", errors="ignore")
                        if "saltmdb" in cmdline_str.lower():
                            is_saltmdb_viewer = True
                    else:
                        res = subprocess.run(
                            ["ps", "-p", str(pid), "-o", "command="],
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        if res.returncode == 0 and res.stdout and "saltmdb" in res.stdout.lower():
                            is_saltmdb_viewer = True
            except Exception:
                is_saltmdb_viewer = False

            if is_saltmdb_viewer:
                import signal

                os.kill(pid, signal.SIGTERM)
                try:
                    os.remove(pid_file)
                except Exception:
                    pass
                return f"Database viewer stopped (PID {pid}) on port {port}."
    except Exception:
        pass
    # Fallback: broad process name match filtered by port
    try:
        if sys.platform == "win32":
            subprocess.run(
                [
                    "powershell",
                    "-Command",
                    f"Get-CimInstance Win32_Process | Where-Object {{ ($_.CommandLine -like '*saltmdb_viewer*' -or $_.CommandLine -like '*saltmdb.viewer*') -and $_.CommandLine -like '*--port {port}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.run(
                ["pkill", "-f", f"saltmdb_viewer.*--port {port}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["pkill", "-f", f"saltmdb.viewer.*--port {port}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return f"Database viewer stopped successfully on port {port} (or was not running)."
    except Exception as e:
        logger.error("Error stopping database viewer: %s", e)
        return f"Error: Database viewer is not running or failed to stop: {e}"


class SALTMDBTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Multithreaded TCPServer subclass that handles concurrent requests and suppresses noisy client disconnect tracebacks."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc_type, exc_value, exc_tb = sys.exc_info()
        if exc_type in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            logger.debug(
                "Client %s disconnected before request completed: %s", client_address, exc_value
            )
            return
        super().handle_error(request, client_address)


def main():
    port = int(os.environ.get("SALTMDB_VIEWER_PORT", 8080))
    for idx, arg in enumerate(sys.argv):
        if arg == "--port" and idx + 1 < len(sys.argv):
            try:
                port = int(sys.argv[idx + 1])
            except ValueError:
                pass

    log_level = os.environ.get("SALTMDB_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, log_level, logging.INFO),
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )

    db_path = get_db_path()
    if not os.path.exists(db_path):
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    logger.info("Starting SALTMDB Viewer on http://localhost:%d", port)
    logger.info("Reading database: %s", db_path)

    SALTMDBTCPServer.allow_reuse_address = True
    try:
        with SALTMDBTCPServer(("127.0.0.1", port), SALTMDBHandler) as httpd:
            watchdog_thread = threading.Thread(
                target=_run_liveness_watchdog,
                args=(httpd, port),
                daemon=True,
            )
            watchdog_thread.start()
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                logger.info("Stopping SALTMDB Viewer.")
    except OSError as e:
        logger.error("Failed to bind viewer to 127.0.0.1:%d: %s", port, e)
        sys.exit(1)


if __name__ == "__main__":
    main()
