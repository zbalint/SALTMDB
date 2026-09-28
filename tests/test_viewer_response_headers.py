import io
import unittest

from saltmdb.viewer.routes import SALTMDBHandler


class DummyRequest:
    def makefile(self, *args, **kwargs):
        return io.BytesIO(b"")


class DummyServer:
    pass


class TestViewerResponseHeaders(unittest.TestCase):
    def _handler_recording_headers(self):
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())
        headers = {}
        handler.wfile = io.BytesIO()
        handler.send_response = lambda code, message=None: None
        handler.send_header = lambda key, value: headers.__setitem__(key, value)
        handler.end_headers = lambda: None
        return handler, headers

    def test_dynamic_json_and_html_are_never_cached_or_sniffed(self):
        for name, send in (
            ("json", lambda h: h.send_json({"secret": "memory body"})),
            ("html", lambda h: h.send_html("<html></html>")),
        ):
            with self.subTest(name):
                handler, headers = self._handler_recording_headers()
                send(handler)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_static_assets_keep_their_public_cache(self):
        handler, headers = self._handler_recording_headers()
        handler.command = "GET"
        handler.send_static_asset("/static/viewer.css")
        self.assertEqual(headers["Cache-Control"], "public, max-age=86400")


if __name__ == "__main__":
    unittest.main()
