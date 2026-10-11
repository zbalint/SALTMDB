"""Read-only MCP tool tester endpoints."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from saltmdb.config import get_viewer_tools_enabled
from saltmdb.daemon.protocol import READ_TOOLS
from saltmdb.viewer.tool_runner import ToolRunnerError

if TYPE_CHECKING:
    from saltmdb.viewer.routes._protocol import ViewerHandlerProtocol
else:
    ViewerHandlerProtocol = object

logger = logging.getLogger(__name__)
MAX_ARGUMENTS_CHARS = 8192
MAX_RESULT_CHARS = 262144


class ToolsMixin(ViewerHandlerProtocol):
    """Provides the tool catalog and read-only tool call endpoints."""

    def get_tools(self) -> None:
        if not self._tool_tester_allowed():
            return
        if not get_viewer_tools_enabled():
            self.send_json({"error": "Tool tester disabled"}, 503)
            return
        try:
            tools = self._tool_runner().list_tools()
        except ToolRunnerError as exc:
            self._send_runner_error(exc)
            return
        payload = []
        for tool in sorted(tools, key=lambda item: item.name):
            access = "read" if tool.name in READ_TOOLS else "write"
            payload.append(
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema,
                    "access": access,
                    "enabled": access == "read",
                }
            )
        self.send_json({"agent_id": "viewer", "tools": payload})

    def get_tool_call(self, query) -> None:  # noqa: PLR0911 -- each request validation/error branch returns its HTTP response
        if not self._tool_tester_allowed():
            return
        if not get_viewer_tools_enabled():
            self.send_json({"error": "Tool tester disabled"}, 503)
            return
        parsed = self._parse_tool_call(query)
        if parsed is None:
            return
        name, arguments = parsed

        started = time.perf_counter()
        try:
            tools = self._tool_runner().list_tools()
            tool = next((item for item in tools if item.name == name), None)
            if tool is None:
                self.send_json({"error": "Tool not found"}, 404)
                return
            if name not in READ_TOOLS:
                self.send_json({"error": "Tool is disabled in the read-only viewer"}, 403)
                return
            result = self._tool_runner().call_tool(name, arguments)
        except ToolRunnerError as exc:
            self._send_runner_error(exc)
            return
        except Exception:
            logger.exception("Viewer MCP tool call failed")
            self.send_json({"error": "Tool tester unavailable"}, 503)
            return

        result_text = "".join(getattr(block, "text", "") for block in result.content)
        is_error = bool(result.isError)
        if not is_error:
            try:
                payload = json.loads(result_text)
                is_error = isinstance(payload, dict) and payload.get("status") == "rejected"
            except json.JSONDecodeError:
                pass
        truncated = len(result_text) > MAX_RESULT_CHARS
        if truncated:
            result_text = result_text[:MAX_RESULT_CHARS]
        self.send_json(
            {
                "tool": name,
                "agent_id": "viewer",
                "arguments": arguments,
                "is_error": is_error,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "result_text": result_text,
                "truncated": truncated,
            }
        )

    def _parse_tool_call(self, query):
        name = query.get("name", [None])[0]
        if not name:
            self.send_json({"error": "name is required"}, 400)
            return None
        raw_arguments = query.get("arguments", [None])[0]
        if raw_arguments is None or len(raw_arguments) > MAX_ARGUMENTS_CHARS:
            self.send_json(
                {"error": "arguments must be a JSON object of at most 8192 characters"}, 400
            )
            return None
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            self.send_json({"error": "arguments must be valid JSON"}, 400)
            return None
        if not isinstance(arguments, dict):
            self.send_json({"error": "arguments must be a JSON object"}, 400)
            return None
        return name, arguments

    def _tool_tester_allowed(self) -> bool:
        headers = getattr(self, "headers", None)
        if headers is None or headers.get("X-SALTMDB-Tool-Tester") != "1":
            self.send_json({"error": "Tool tester header required"}, 403)
            return False
        fetch_site = headers.get("Sec-Fetch-Site")
        if fetch_site is not None and fetch_site != "same-origin":
            self.send_json({"error": "Cross-site tool tester request refused"}, 403)
            return False
        host = headers.get("Host")
        if not host:
            self.send_json({"error": "Host header required"}, 403)
            return False
        origin = headers.get("Origin")
        if origin is not None and origin != f"http://{host}":
            self.send_json({"error": "Origin does not match Host"}, 403)
            return False
        port = getattr(getattr(self.server, "daemon_state", None), "viewer_port", None)
        if port is None:
            port = getattr(self.server, "server_address", (None, None))[1]
        if host not in {f"localhost:{port}", f"127.0.0.1:{port}"}:
            self.send_json({"error": "Host does not match viewer"}, 403)
            return False
        return True

    def _tool_runner(self):
        runner = getattr(self.server, "tool_runner", None)
        if runner is None:
            raise ToolRunnerError("unavailable", "tool tester runner is unavailable")
        return runner

    def _send_runner_error(self, error: ToolRunnerError) -> None:
        if error.kind == "unavailable" and str(error) == "busy":
            self.send_json({"error": "busy"}, 503)
        elif error.kind == "timeout":
            self.send_json({"error": "timeout"}, 504)
        else:
            self.send_json({"error": "Tool tester unavailable"}, 503)
