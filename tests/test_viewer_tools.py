import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from saltmdb.daemon import discovery
from saltmdb.daemon.protocol import READ_TOOLS, WRITE_TOOLS
from saltmdb.viewer.routes import SALTMDBHandler
from saltmdb.viewer.tool_runner import ToolRunner

FIXTURE = Path(__file__).parent / "fixtures" / "viewer_tools_server.py"


class DummyRequest:
    def makefile(self, *args, **kwargs):
        import io

        return io.BytesIO(b"")


class DummyState:
    viewer_port = 8080


class DummyServer:
    server_address = ("127.0.0.1", 8080)

    def __init__(self, runner):
        self.daemon_state = DummyState()
        self.tool_runner = runner


class TestViewerToolsRoutes(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.journal = self.root / "calls.jsonl"
        self.runner = ToolRunner(
            str(self.root / "viewer.db"),
            command=[sys.executable, str(FIXTURE.resolve())],
            env={**os.environ, "VIEWER_TOOLS_JOURNAL": str(self.journal)},
        )

    def tearDown(self):
        self.runner.close()
        self.temp_dir.cleanup()

    def _handler(self, headers=None):
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 0), DummyServer(self.runner))
        handler.headers = (
            headers
            if headers is not None
            else {
                "X-SALTMDB-Tool-Tester": "1",
                "Host": "localhost:8080",
                "Sec-Fetch-Site": "same-origin",
                "Origin": "http://localhost:8080",
            }
        )
        captured = {}
        handler.send_json = lambda data, status=200: captured.update(data=data, status=status)
        return handler, captured

    @staticmethod
    def _lines(path):
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    def _call(self, handler, name, arguments=None):
        handler.get_tool_call({"name": [name], "arguments": [json.dumps(arguments or {})]})

    def test_catalog_call_classification_and_rejections(self):
        handler, catalog = self._handler()
        handler.get_tools()
        self.assertEqual(catalog["status"], 200)
        tools = {tool["name"]: tool for tool in catalog["data"]["tools"]}
        self.assertEqual(set(tools), {"mystery_tool", "search_tags", "store_memory"})
        self.assertEqual(tools["search_tags"]["access"], "read")
        self.assertTrue(tools["search_tags"]["enabled"])
        self.assertEqual(tools["store_memory"]["access"], "write")
        self.assertFalse(tools["store_memory"]["enabled"])
        self.assertEqual(tools["mystery_tool"]["access"], "write")

        handler, called = self._handler()
        self._call(handler, "search_tags")
        self.assertEqual(called["status"], 200)
        self.assertFalse(called["data"]["is_error"])
        self.assertIn("fixture", called["data"]["result_text"])
        call_count = len(self._lines(self.journal))
        for name in ("store_memory", "mystery_tool"):
            handler, rejected = self._handler()
            self._call(handler, name, {"content": "not sent"})
            self.assertEqual(
                rejected,
                {"data": {"error": "Tool is disabled in the read-only viewer"}, "status": 403},
            )
        self.assertEqual(len(self._lines(self.journal)), call_count)

        handler, unknown = self._handler()
        self._call(handler, "missing_tool")
        self.assertEqual(unknown["status"], 404)

    def test_validation_security_flag_closed_and_truncation(self):
        for query in (
            {"arguments": ["not-json"], "name": ["search_tags"]},
            {"arguments": ["[]"], "name": ["search_tags"]},
            {"arguments": ["x" * 8193], "name": ["search_tags"]},
            {"arguments": ["{}"]},
        ):
            handler, captured = self._handler()
            handler.get_tool_call(query)
            self.assertEqual(captured["status"], 400)

        security_headers = (
            {},
            {"X-SALTMDB-Tool-Tester": "1", "Sec-Fetch-Site": "same-origin"},
            {
                "X-SALTMDB-Tool-Tester": "1",
                "Host": "localhost:8080",
                "Sec-Fetch-Site": "cross-site",
            },
            {"X-SALTMDB-Tool-Tester": "1", "Host": "evil:8080", "Sec-Fetch-Site": "same-origin"},
            {"X-SALTMDB-Tool-Tester": "1", "Host": "localhost:8080", "Origin": "http://evil:8080"},
        )
        for headers in security_headers:
            handler, captured = self._handler(headers)
            handler.get_tools()
            self.assertEqual(captured["status"], 403)
        self.assertFalse(self.journal.exists())

        os.environ["SALTMDB_VIEWER_TOOLS_ENABLED"] = "false"
        try:
            handler, captured = self._handler()
            handler.get_tools()
            self.assertEqual(captured, {"data": {"error": "Tool tester disabled"}, "status": 503})
            handler, captured = self._handler()
            self._call(handler, "search_tags")
            self.assertEqual(captured, {"data": {"error": "Tool tester disabled"}, "status": 503})
        finally:
            os.environ.pop("SALTMDB_VIEWER_TOOLS_ENABLED", None)

        handler, captured = self._handler()
        self._call(handler, "search_tags", {"mode": "large"})
        self.assertEqual(captured["status"], 200)
        self.assertTrue(captured["data"]["truncated"])
        self.assertEqual(len(captured["data"]["result_text"]), 262144)

    def test_busy_and_closed_runner_are_fail_fast(self):
        first_handler, first_result = self._handler()
        first = threading.Thread(
            target=self._call,
            args=(first_handler, "search_tags", {"delay": 0.5}),
            daemon=True,
        )
        first.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.journal.exists():
            time.sleep(0.02)
        second_handler, second_result = self._handler()
        self._call(second_handler, "search_tags")
        self.assertEqual(second_result, {"data": {"error": "busy"}, "status": 503})
        first.join(timeout=5)
        self.assertFalse(first.is_alive())
        self.assertEqual(first_result["status"], 200)

        self.runner.close()
        closed_handler, closed_result = self._handler()
        closed_handler.get_tools()
        self.assertEqual(
            closed_result, {"data": {"error": "Tool tester unavailable"}, "status": 503}
        )

    def test_post_remains_read_only(self):
        handler, captured = self._handler()
        handler.do_POST()
        self.assertEqual(captured, {"data": {"error": "Viewer is read-only"}, "status": 405})


class TestViewerToolsRealAdapter(unittest.TestCase):
    def test_real_adapter_exposes_the_complete_read_write_catalog(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            runtime = home / "runtime"
            runtime.mkdir(parents=True)
            runtime.chmod(0o700)
            env = {
                **os.environ,
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / "config"),
                "XDG_STATE_HOME": str(home / "state"),
                "XDG_RUNTIME_DIR": str(runtime),
                "SALTMDB_DB_PATH": str(root / "viewer.db"),
                "SALTMDB_VIEWER_ENABLED": "false",
            }
            with patch.dict(os.environ, env, clear=False):
                canonical = discovery.resolve_canonical_db_path(env["SALTMDB_DB_PATH"])
                discovery_file = discovery.discovery_path(discovery.daemon_key(canonical))
            Path(discovery_file).unlink(missing_ok=True)
            daemon = subprocess.Popen(
                [sys.executable, "-m", "saltmdb.daemon.server", "--foreground"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            runner = None
            try:
                deadline = time.monotonic() + 30
                while not Path(discovery_file).exists() and time.monotonic() < deadline:
                    if daemon.poll() is not None:
                        assert daemon.stdout is not None
                        output = daemon.stdout.read().decode("utf-8", errors="replace")
                        self.fail(f"isolated daemon exited before discovery: {output}")
                    time.sleep(0.1)
                self.assertTrue(Path(discovery_file).exists())
                runner = ToolRunner(
                    env["SALTMDB_DB_PATH"],
                    command=[sys.executable, "-m", "saltmdb"],
                    env=env,
                )
                tools = runner.list_tools()
                names = {tool.name for tool in tools}
                self.assertEqual(len(names), 24)
                self.assertTrue(names <= READ_TOOLS | WRITE_TOOLS)
                result = runner.call_tool("search_tags", {})
                self.assertFalse(result.isError)
            finally:
                if runner is not None:
                    runner.close()
                if daemon.poll() is None:
                    daemon.send_signal(signal.SIGTERM)
                    try:
                        daemon.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
                        daemon.wait(timeout=5)
                Path(discovery_file).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
