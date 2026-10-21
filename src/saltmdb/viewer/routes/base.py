"""Core HTTP plumbing for the SALTMDB Viewer request handler.

``ViewerHandlerBase`` owns everything that isn't a single API endpoint's business
logic: request lifecycle, response helpers, static-asset serving, and the top-level
GET/POST/HEAD/OPTIONS dispatch table. The concrete ``SALTMDBHandler`` (assembled in
``saltmdb.viewer.routes.__init__``) mixes this in alongside one feature mixin per
endpoint group; every ``self.get_*`` call below is resolved through that class's MRO.
"""

import http.server
import json
import logging
import os
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

from saltmdb.config import get_db_path
from saltmdb.viewer.context import ViewerReadGateway
from saltmdb.viewer.routes._shared import STATIC_ASSETS
from saltmdb.viewer.templates import get_frontend_html

if TYPE_CHECKING:
    from saltmdb.viewer.routes._protocol import ViewerHandlerProtocol
else:
    ViewerHandlerProtocol = object

logger = logging.getLogger(__name__)
_ALLOWED_HOSTNAMES = ("localhost", "127.0.0.1", "[::1]")
_MISDIRECTED_STATUS = 421
_MISDIRECTED_MESSAGE = "Misdirected Request"
_ENTITIES_API_PREFIX = "/api/entities/"


class ViewerHandlerBase(http.server.BaseHTTPRequestHandler, ViewerHandlerProtocol):
    """Zero-dependency HTTP Request Handler for the SALTMDB Dashboard Viewer."""

    def log_message(self, format, *args):
        # Access logging is intentionally disabled; Viewer requests may contain memory content.
        pass

    def _host_allowed(self) -> bool:
        headers = getattr(self, "headers", None)
        host_values = headers.get_all("Host") if headers else None
        if (
            not isinstance(host_values, list)
            or len(host_values) != 1
            or not isinstance(host_values[0], str)
        ):
            return False
        value = host_values[0].strip(" \t")
        address = getattr(getattr(self, "server", None), "server_address", None)
        if not isinstance(address, tuple) or len(address) < 2:
            return False
        if value.startswith("["):
            closing_bracket = value.find("]")
            if closing_bracket < 0:
                return False
            host = value[: closing_bracket + 1].lower()
            remainder = value[closing_bracket + 1 :]
            if not remainder.startswith(":"):
                return False
            port_text = remainder[1:]
        else:
            separator = value.rfind(":")
            host = value[:separator].lower()
            if separator <= 0:
                return False
            port_text = value[separator + 1 :]
        return host in _ALLOWED_HOSTNAMES and port_text == str(address[1])

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        if not self._host_allowed():
            self.send_error(_MISDIRECTED_STATUS, _MISDIRECTED_MESSAGE)
            return False
        return True

    def handle_expect_100(self) -> bool:
        if not self._host_allowed():
            self.send_error(_MISDIRECTED_STATUS, _MISDIRECTED_MESSAGE)
            return False
        return super().handle_expect_100()

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError) as e:
            self.close_connection = True
            logger.debug("Client connection aborted during request: %s", e)

    def _send_dynamic_headers(self):
        """Headers for responses that can carry memory content: never cached, never sniffed."""
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def send_json(self, data, status=200):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_dynamic_headers()
            headers = getattr(self, "headers", None)
            origin = headers.get("Origin", "") if headers else ""
            if origin:
                parsed_origin = urllib.parse.urlparse(origin)
                if parsed_origin.hostname in ("localhost", "127.0.0.1"):
                    self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.debug("Client disconnected before JSON response was sent: %s", e)

    def send_html(self, html_content, status=200):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._send_dynamic_headers()
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(html_content.encode("utf-8"))
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.debug("Client disconnected before HTML response was sent: %s", e)

    def send_static_asset(self, path):
        """Serve one local Viewer asset without exposing arbitrary filesystem paths."""
        root = (Path(__file__).resolve().parent.parent / "static").resolve()
        asset = STATIC_ASSETS.get(path)
        if asset is None:
            self.send_json({"error": "Asset not found"}, 404)
            return
        relative_path, content_type = asset
        candidate = root / relative_path
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(candidate.read_bytes())

    def do_OPTIONS(self):
        try:
            self.send_response(200)
            headers = getattr(self, "headers", None)
            origin = headers.get("Origin", "") if headers else ""
            if origin:
                parsed_origin = urllib.parse.urlparse(origin)
                if parsed_origin.hostname in ("localhost", "127.0.0.1"):
                    self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.debug("Client disconnected during OPTIONS request: %s", e)

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)
        if self._dispatch_get_api(path, query):
            return
        if self._dispatch_get_entity(path, query):
            return
        if path == "/" or path == "/index.html":  # noqa: PLR1714
            self.send_html(get_frontend_html())
            return
        self.send_json({"error": "Endpoint not found"}, 404)

    def _dispatch_get_api(self, path, query):
        for dispatcher in (
            self._dispatch_get_primary,
            self._dispatch_get_sessions,
            self._dispatch_get_relations,
            self._dispatch_get_secondary,
        ):
            if dispatcher(path, query):
                return True
        return False

    def _dispatch_get_primary(self, path, query):
        if path.startswith("/static/"):
            self.send_static_asset(path)
        elif path == "/api/embeddings_stats":
            self.get_embeddings_stats()
        elif path == "/api/scatterplot":
            self.get_scatterplot()
        elif path in ("/api/entities", "/api/entity"):
            self.get_entities(query)
        elif path == "/api/events":
            self.get_events(query)
        elif path == "/api/feed":
            self.get_feed(query)
        elif path == "/api/tags":
            self.get_tags()
        else:
            return False
        return True

    def _dispatch_get_sessions(self, path, query):
        if path == "/api/sessions":
            self.get_sessions(query)
        elif path.startswith("/api/sessions/"):
            self.get_session_detail(urllib.parse.unquote(path[len("/api/sessions/") :]))
        elif path == "/api/traces":
            self.get_traces(query)
        elif path.startswith("/api/traces/"):
            self.get_trace_detail(urllib.parse.unquote(path[len("/api/traces/") :]))
        else:
            return False
        return True

    def _dispatch_get_relations(self, path, query):
        if path == "/api/locks":
            self.send_json(
                {"error": "System Locks was retired", "replacement": "/api/operations"}, 410
            )
        elif path == "/api/relations":
            self.get_all_relations(query)
        elif path == "/api/relations/neighborhood":
            self.get_relations_neighborhood(query)
        elif path == "/api/relations/graph":
            self.get_relations_graph(query)
        else:
            return False
        return True

    def _dispatch_get_secondary(self, path, query):
        if path == "/api/stats":
            self.get_stats()
        elif path == "/api/operations":
            self.get_operations()
        elif path == "/api/quality":
            self.get_quality(query)
        elif path == "/api/search":
            self.get_search(query)
        else:
            return False
        return True

    def _dispatch_get_entity(self, path, query):
        if not (path.startswith(_ENTITIES_API_PREFIX) or path.startswith("/api/entity/")):
            return False
        prefix = _ENTITIES_API_PREFIX if path.startswith(_ENTITIES_API_PREFIX) else "/api/entity/"
        raw_subpath = path[len(prefix) :]
        if raw_subpath.endswith("/lineage"):
            self.get_lineage(urllib.parse.unquote(raw_subpath[: -len("/lineage")]))
        elif raw_subpath.endswith("/relations"):
            self.get_entity_relations(
                urllib.parse.unquote(raw_subpath[: -len("/relations")]), query
            )
        else:
            self.get_entity_detail(urllib.parse.unquote(raw_subpath))
        return True

    def do_POST(self):
        self.send_json({"error": "Viewer is read-only"}, 405)

    def do_HEAD(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/static/"):
            self.send_static_asset(path)
        else:
            self.send_json({"error": "Endpoint not found"}, 404)

    def get_db_connection(self):
        gateway = getattr(self.server, "viewer_gateway", None)
        if isinstance(gateway, ViewerReadGateway):
            return gateway.connect()
        # Test-only fallback: production construction always injects a gateway.
        db_path = os.environ.get("SALTMDB_DB_PATH") or get_db_path()
        return ViewerReadGateway(db_path).connect()
