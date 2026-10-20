import http.server
import io
import json
import socket
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from saltmdb.viewer.routes import SALTMDBHandler, STATIC_ASSETS

STATIC_ROOT = Path(__file__).resolve().parents[1] / "src/saltmdb/viewer/static"
ASSETS = (
    ("/static/viewer.css", "viewer.css", "text/css; charset=utf-8"),
    ("/static/viewer.js", "viewer.js", "text/javascript; charset=utf-8"),
    (
        "/static/vendor/marked-18.0.7.umd.js",
        "vendor/marked-18.0.7.umd.js",
        "text/javascript; charset=utf-8",
    ),
    (
        "/static/vendor/dompurify-3.4.16.min.js",
        "vendor/dompurify-3.4.16.min.js",
        "text/javascript; charset=utf-8",
    ),
)


class DummyRequest:
    def makefile(self, *args: object, **kwargs: object) -> io.BytesIO:
        del args, kwargs
        return io.BytesIO(b"")


class DummyServer:
    pass


class TestViewerStaticAssets(unittest.TestCase):
    def _handler_recording_headers(self):
        handler = SALTMDBHandler(
            cast(socket.socket, cast(object, DummyRequest())),
            ("127.0.0.1", 8080),
            cast(http.server.HTTPServer, cast(object, DummyServer())),
        )
        headers: dict[str, str] = {}
        statuses: list[int] = []
        handler.wfile = io.BytesIO()

        def record_response(code: int, message: str | None = None) -> None:
            del message
            statuses.append(code)

        def record_header(keyword: str, value: str) -> None:
            headers[keyword] = value

        handler.send_response = record_response
        handler.send_header = record_header
        handler.end_headers = lambda: None
        return handler, headers, statuses

    def test_static_table_pairs_have_existing_paths_and_explicit_types(self):
        self.assertEqual(set(STATIC_ASSETS), {path for path, _, _ in ASSETS})
        for path, asset in STATIC_ASSETS.items():
            with self.subTest(path=path):
                self.assertIsInstance(asset, tuple)
                self.assertEqual(len(asset), 2)
                relative_path, content_type = asset
                candidate = (STATIC_ROOT / relative_path).resolve()
                self.assertTrue(candidate.is_relative_to(STATIC_ROOT.resolve()))
                self.assertTrue(candidate.is_file())
                self.assertIn(
                    content_type,
                    ("text/css; charset=utf-8", "text/javascript; charset=utf-8"),
                )

    def test_static_get_and_head_preserve_bytes_and_safe_headers(self):
        for path, relative_path, content_type in ASSETS:
            for method in ("GET", "HEAD"):
                with self.subTest(path=path, method=method):
                    handler, headers, statuses = self._handler_recording_headers()
                    handler.command = method
                    handler.send_static_asset(path)  # pyright: ignore[reportUnknownMemberType]
                    self.assertEqual(statuses, [200])
                    self.assertEqual(headers["Content-Type"], content_type)
                    self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                    self.assertEqual(headers["Cache-Control"], "public, max-age=86400")
                    expected = (
                        (STATIC_ROOT / relative_path).read_bytes() if method == "GET" else b""
                    )
                    self.assertEqual(cast(io.BytesIO, handler.wfile).getvalue(), expected)

    def test_routing_preserves_cache_busting_queries(self):
        for target, relative_path, content_type in (
            ("/static/viewer.js?v=1", "viewer.js", "text/javascript; charset=utf-8"),
            ("/static/viewer.css?v=2", "viewer.css", "text/css; charset=utf-8"),
        ):
            for method in ("GET", "HEAD"):
                with self.subTest(target=target, method=method):
                    handler, headers, statuses = self._handler_recording_headers()
                    handler.command = method
                    handler.path = target
                    getattr(handler, f"do_{method}")()
                    self.assertEqual(statuses, [200])
                    self.assertEqual(headers["Content-Type"], content_type)
                    self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                    self.assertEqual(headers["Cache-Control"], "public, max-age=86400")
                    expected = (
                        (STATIC_ROOT / relative_path).read_bytes() if method == "GET" else b""
                    )
                    self.assertEqual(cast(io.BytesIO, handler.wfile).getvalue(), expected)

    def test_routing_rejects_unlisted_paths_without_reading_files(self):
        for target in (
            "/static/%2e%2e/x",
            "/static/../../etc/passwd",
            "/static/viewer.js/",
            "/static/nope.js",
        ):
            for method in ("GET", "HEAD"):
                with self.subTest(target=target, method=method):
                    handler, _, statuses = self._handler_recording_headers()
                    handler.command = method
                    handler.path = target
                    with patch.object(Path, "read_bytes") as read_bytes:
                        getattr(handler, f"do_{method}")()
                    self.assertEqual(statuses, [404])
                    read_bytes.assert_not_called()
                    if method == "GET":
                        self.assertEqual(
                            json.loads(cast(io.BytesIO, handler.wfile).getvalue()),
                            {"error": "Asset not found"},
                        )

    def test_routing_double_slash_does_not_dispatch_static_asset(self):
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                handler, _, statuses = self._handler_recording_headers()
                handler.command = method
                handler.path = "//static/viewer.js"
                with patch.object(handler, "send_static_asset") as send_static_asset:
                    getattr(handler, f"do_{method}")()
                self.assertEqual(statuses, [404])
                send_static_asset.assert_not_called()
