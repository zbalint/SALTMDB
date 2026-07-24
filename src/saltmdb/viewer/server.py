import sys
import os
import socket
import socketserver
import subprocess
import time
import logging
import urllib.request
from saltmdb.config import get_db_path, VIEWER_SHIM_PATH
from saltmdb.viewer.routes import SALTMDBHandler

logger = logging.getLogger(__name__)

def start_viewer(port: int = 8080) -> str:
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
        
        popen_kwargs = {
            "stdout": log_file,
            "stderr": log_file,
            "env": env
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        else:
            popen_kwargs["start_new_session"] = True
            
        process = subprocess.Popen(viewer_cmd, **popen_kwargs)
        log_file.close()

        # Store PID for clean shutdown
        try:
            pid_file = os.path.join(os.path.expanduser("~/.saltmdb"), "viewer.pid")
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

def stop_viewer(port: int = 8080) -> str:
    """Stops the running local SALTMDB web dashboard/viewer."""
    port = port or 8080
    
    # Try PID-based termination first (precise, no false positives)
    try:
        pid_file = os.path.join(os.path.expanduser("~/.saltmdb"), "viewer.pid")
        if os.path.exists(pid_file):
            with open(pid_file) as pf:
                pid = int(pf.read().strip())
            import signal
            os.kill(pid, signal.SIGTERM)
            try:
                os.remove(pid_file)
            except Exception:
                pass
            return f"Database viewer stopped (PID {pid}) on port {port}."
    except Exception:
        pass
    # Fallback: broad process name match
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["powershell", "-Command", f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*saltmdb_viewer*' -or $_.CommandLine -like '*saltmdb.viewer*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        else:
            subprocess.run(["pkill", "-f", "saltmdb_viewer"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-f", "saltmdb.viewer"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Database viewer stopped successfully on port {port} (or was not running)."
    except Exception as e:
        logger.error("Error stopping database viewer: %s", e)
        return f"Error: Database viewer is not running or failed to stop: {e}"

class SALTMDBTCPServer(socketserver.TCPServer):
    """TCPServer subclass that suppresses noisy tracebacks for expected client disconnects."""
    def handle_error(self, request, client_address):
        exc_type, exc_value, exc_tb = sys.exc_info()
        if exc_type in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            logger.debug("Client %s disconnected before request completed: %s", client_address, exc_value)
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

    db_path = get_db_path()
    if not os.path.exists(db_path):
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    logger.info("Starting SALTMDB Viewer on http://localhost:%d", port)
    logger.info("Reading database: %s", db_path)

    SALTMDBTCPServer.allow_reuse_address = True
    with SALTMDBTCPServer(("127.0.0.1", port), SALTMDBHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            logger.info("Stopping SALTMDB Viewer.")

if __name__ == "__main__":
    main()
