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
import mimetypes
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


class ViewerHandlerBase(http.server.BaseHTTPRequestHandler, ViewerHandlerProtocol):
    """Zero-dependency HTTP Request Handler for the SALTMDB Dashboard Viewer."""

    def log_message(self, format, *args):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError) as e:
            self.close_connection = True
            logger.debug("Client connection aborted during request: %s", e)

    def send_json(self, data, status=200):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
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
        relative_path = STATIC_ASSETS.get(path)
        if relative_path is None:
            self.send_json({"error": "Asset not found"}, 404)
            return
        candidate = root / relative_path
        mime_type, _ = mimetypes.guess_type(candidate.name)
        self.send_response(200)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Cache-Control", "public, max-age=86400")
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

    def do_GET(self):  # noqa: C901, PLR0912, PLR0915
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

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
        elif path == "/api/tags":
            self.get_tags()
        elif path == "/api/sessions":
            self.get_sessions(query)
        elif path.startswith("/api/sessions/"):
            self.get_session_detail(urllib.parse.unquote(path[len("/api/sessions/") :]))
        elif path == "/api/locks":
            self.send_json(
                {"error": "System Locks was retired", "replacement": "/api/operations"}, 410
            )
        elif path == "/api/relations":
            self.get_all_relations(query)
        elif path == "/api/relations/neighborhood":
            self.get_relations_neighborhood(query)
        elif path == "/api/relations/graph":
            self.get_relations_graph(query)
        elif path == "/api/stats":
            self.get_stats()
        elif path == "/api/operations":
            self.get_operations()
        elif path == "/api/quality":
            self.get_quality(query)
        elif path == "/api/search":
            self.get_search(query)
        elif path.startswith("/api/entities/") or path.startswith("/api/entity/"):
            prefix = "/api/entities/" if path.startswith("/api/entities/") else "/api/entity/"
            raw_subpath = path[len(prefix) :]
            if raw_subpath.endswith("/lineage"):
                eid = urllib.parse.unquote(raw_subpath[: -len("/lineage")])
                self.get_lineage(eid)
            elif raw_subpath.endswith("/relations"):
                eid = urllib.parse.unquote(raw_subpath[: -len("/relations")])
                self.get_entity_relations(eid, query)
            else:
                entity_id = urllib.parse.unquote(raw_subpath)
                self.get_entity_detail(entity_id)
        elif path == "/" or path == "/index.html":  # noqa: PLR1714
            self.send_html(get_frontend_html())
        else:
            self.send_json({"error": "Endpoint not found"}, 404)

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
