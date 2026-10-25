import io
import socket
import threading
import unittest
from email.message import Message

from saltmdb.viewer.routes import SALTMDBHandler
from saltmdb.viewer.server import SALTMDBTCPServer


class DummyRequest:
    def makefile(self, *args, **kwargs):
        return io.BytesIO(b"")


class DummyServer:
    pass


class CorsOriginHandlerTests(unittest.TestCase):
    allowed_origins = (
        "http://localhost",
        "https://localhost",
        "http://localhost:1",
        "https://localhost:80",
        "http://localhost:8765",
        "http://127.0.0.1",
        "http://127.0.0.1:3000",
        "https://127.0.0.1:65535",
    )
    rejected_origins = (
        "http://evil.example",
        "http://localhost.evil.example",
        "http://localhost@evil.example",
        "null",
        "http://[::1]:8765",
        "http://127a0a0a1",
        "http://localhost:0",
        "http://localhost:08080",
        "http://localhost:00",
        "http://localhost:65536",
        "http://localhost:99999",
        "http://127.0.0.1:010",
        "http://localhost:٨٧",
        "HTTP://localhost",
        "http://localhost/",
        "http://localhost/path",
        "http://localhost?x=1",
        "http://localhost:",
        "http://localhost:abc",
        "http://localhost:123456",
        "ftp://localhost",
        "http:/localhost",
        "http:///localhost",
        " http://localhost",
        "http://localhost\n",
        "http://localhost\r\nX-Evil: 1",
        "http://localhost/\r\n X-Evil: 1",
    )

    def _handler_recording_headers(self, origin=None, *, include_headers=True):
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())
        sent = []
        handler.wfile = io.BytesIO()
        handler.send_response = lambda code, message=None: None
        handler.send_header = lambda key, value: sent.append((key, value))
        handler.end_headers = lambda: None
        if include_headers:
            handler.headers = Message()
            if origin is not None:
                handler.headers["Origin"] = origin
        return handler, sent

    def _invoke(self, method, origin=None, *, include_headers=True):
        handler, sent = self._handler_recording_headers(origin, include_headers=include_headers)
        if method == "send_json":
            handler.send_json({})
        else:
            handler.do_OPTIONS()
        return sent

    def test_allowed_origins_are_echoed_for_both_response_methods(self):
        for method in ("send_json", "do_OPTIONS"):
            for origin in self.allowed_origins:
                with self.subTest(method=method, origin=origin):
                    sent = dict(self._invoke(method, origin))
                    self.assertEqual(sent["Access-Control-Allow-Origin"], origin)

    def test_send_json_rejects_noncanonical_origins(self):
        for origin in self.rejected_origins:
            with self.subTest(origin=repr(origin)):
                sent = dict(self._invoke("send_json", origin))
                self.assertNotIn("Access-Control-Allow-Origin", sent)

        for label, kwargs in (
            ("missing header", {"origin": None}),
            ("missing headers attribute", {"origin": None, "include_headers": False}),
        ):
            with self.subTest(origin=label):
                sent = dict(self._invoke("send_json", **kwargs))
                self.assertNotIn("Access-Control-Allow-Origin", sent)

    def test_options_rejects_noncanonical_origins(self):
        for origin in self.rejected_origins:
            with self.subTest(origin=repr(origin)):
                sent = dict(self._invoke("do_OPTIONS", origin))
                self.assertNotIn("Access-Control-Allow-Origin", sent)

        for label, kwargs in (
            ("missing header", {"origin": None}),
            ("missing headers attribute", {"origin": None, "include_headers": False}),
        ):
            with self.subTest(origin=label):
                sent = dict(self._invoke("do_OPTIONS", **kwargs))
                self.assertNotIn("Access-Control-Allow-Origin", sent)

    def test_response_headers_keep_existing_contracts(self):
        json_headers = dict(self._invoke("send_json", "http://localhost"))
        self.assertEqual(json_headers["Cache-Control"], "no-store")

        for origin in (None, "http://localhost"):
            with self.subTest(origin=origin):
                options_headers = dict(self._invoke("do_OPTIONS", origin))
                self.assertEqual(options_headers["Access-Control-Allow-Methods"], "GET, OPTIONS")
                self.assertEqual(options_headers["Access-Control-Allow-Headers"], "Content-Type")

    def test_cors_header_stays_at_existing_position(self):
        json_headers = self._invoke("send_json", "http://localhost")
        json_names = [name for name, _ in json_headers]
        self.assertGreater(
            json_names.index("Access-Control-Allow-Origin"), json_names.index("Referrer-Policy")
        )

        options_headers = self._invoke("do_OPTIONS", "http://localhost")
        options_names = [name for name, _ in options_headers]
        self.assertLess(
            options_names.index("Access-Control-Allow-Origin"),
            options_names.index("Access-Control-Allow-Methods"),
        )


class CorsOriginServerTests(unittest.TestCase):
    def setUp(self):
        self.server = SALTMDBTCPServer(("127.0.0.1", 0), SALTMDBHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()

    def _raw_request(self, request):
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as client:
            client.sendall(request)
            client.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)

    def _request_with_origins(self, method, origins):
        path = "/no-such-endpoint" if method == "GET" else "/"
        origin_headers = "".join(f"Origin: {origin}\r\n" for origin in origins)
        return self._raw_request(
            (
                f"{method} {path} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"{origin_headers}"
                "Connection: close\r\n\r\n"
            ).encode()
        )

    def _request_with_origin(self, method, origin):
        return self._request_with_origins(method, (origin,))

    def _request_with_folded_origin(self, method):
        path = "/no-such-endpoint" if method == "GET" else "/"
        return self._raw_request(
            (
                f"{method} {path} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                "Origin: http://localhost/\r\n"
                " X-Evil: 1\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
        )

    def test_folded_origin_is_not_reflected_by_get(self):
        response = self._request_with_folded_origin("GET").lower()
        self.assertNotIn(b"x-evil", response)
        self.assertNotIn(b"access-control-allow-origin", response)

    def test_folded_origin_is_not_reflected_by_options(self):
        response = self._request_with_folded_origin("OPTIONS").lower()
        self.assertNotIn(b"x-evil", response)
        self.assertNotIn(b"access-control-allow-origin", response)

    def test_duplicate_origins_keep_the_first_value(self):
        for method in ("GET", "OPTIONS"):
            allowed_origin = f"http://localhost:{self.port}"
            with self.subTest(method=method, order="allowed-first"):
                response = self._request_with_origins(
                    method, (allowed_origin, "http://evil.example")
                )
                line = f"Access-Control-Allow-Origin: {allowed_origin}\r\n".encode()
                self.assertEqual(response.count(b"Access-Control-Allow-Origin:"), 1)
                self.assertEqual(response.count(line), 1)

            with self.subTest(method=method, order="foreign-first"):
                response = self._request_with_origins(
                    method, ("http://evil.example", allowed_origin)
                )
                self.assertNotIn(b"access-control-allow-origin:", response.lower())

    def test_canonical_origin_is_reflected_once_by_get(self):
        origin = f"http://localhost:{self.port}"
        response = self._request_with_origin("GET", origin)
        line = f"Access-Control-Allow-Origin: {origin}\r\n".encode()
        self.assertEqual(response.count(line), 1)

    def test_canonical_origin_is_reflected_once_by_options(self):
        origin = f"http://localhost:{self.port}"
        response = self._request_with_origin("OPTIONS", origin)
        line = f"Access-Control-Allow-Origin: {origin}\r\n".encode()
        self.assertEqual(response.count(line), 1)


if __name__ == "__main__":
    unittest.main()
