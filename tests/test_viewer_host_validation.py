import http.client
import socket
import threading
import unittest
from email.message import Message
from typing import cast, final
from types import SimpleNamespace
from unittest.mock import Mock, patch

from saltmdb.viewer.routes import SALTMDBHandler
from saltmdb.viewer.server import SALTMDBTCPServer


LOOPBACK_HOST = "127.0.0.1"
LOCALHOST_HOSTNAME = "localhost"
BAD_HOSTNAME = "attacker.example"
ALT_BAD_HOSTNAME = "bad.example"
MISDIRECTED_MESSAGE = "Misdirected Request"
MISDIRECTED_STATUS = 421
MISDIRECTED_STATUS_LINE = b" 421 "
STATIC_PATH = "/static/viewer.css"
API_PATH = "/api/stats"


@final
class HostValidationServerTests(unittest.TestCase):
    server: SALTMDBTCPServer = cast(SALTMDBTCPServer, object())
    thread: threading.Thread = cast(threading.Thread, object())
    port = 0

    def setUp(self):
        self.server = SALTMDBTCPServer((LOOPBACK_HOST, 0), SALTMDBHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()

    def _request(self, method="GET", path=STATIC_PATH, host=None, headers=None):
        connection = http.client.HTTPConnection(LOOPBACK_HOST, self.port, timeout=3)
        request_headers = {} if headers is None else dict(headers)
        if host is not None:
            request_headers["Host"] = host
        connection.request(method, path, headers=request_headers)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def _raw_request(self, request):
        with socket.create_connection((LOOPBACK_HOST, self.port), timeout=3) as client:
            client.sendall(request)
            client.shutdown(socket.SHUT_WR)
            return client.recv(65536)

    def test_loopback_hosts_are_accepted_for_get_and_head(self):
        accepted = (
            f"{LOOPBACK_HOST}:{self.port}",
            f"{LOCALHOST_HOSTNAME}:{self.port}",
            f"LOCALHOST:{self.port}",
            f"[::1]:{self.port}",
        )
        for host in accepted:
            with self.subTest(host=host):
                status, _ = self._request(host=host)
                self.assertEqual(status, 200)
                head_status, body = self._request(method="HEAD", host=host)
                self.assertEqual(head_status, 200)
                self.assertEqual(body, b"")

    def test_default_host_is_accepted(self):
        status, _ = self._request()
        self.assertEqual(status, 200)

    def test_bad_hosts_are_rejected_before_dispatch(self):
        bad_hosts = (
            f"{BAD_HOSTNAME}:{self.port}",
            LOOPBACK_HOST,
            f"{LOOPBACK_HOST}:{self.port + 1}",
            f"{LOCALHOST_HOSTNAME}.{BAD_HOSTNAME}:{self.port}",
            f"{LOOPBACK_HOST}:abc",
            "",
            f"{LOCALHOST_HOSTNAME}:+{self.port}",
            f"{LOCALHOST_HOSTNAME}:0{self.port}",
            f"{LOCALHOST_HOSTNAME}.:{self.port}",
            f"user@{LOCALHOST_HOSTNAME}:{self.port}",
            f"evil-{LOCALHOST_HOSTNAME}:{self.port}",
            f"{LOOPBACK_HOST}.{BAD_HOSTNAME}:{self.port}",
            f"::1:{self.port}",
            "[::1]",
            f"{LOCALHOST_HOSTNAME}:",
            f"{LOCALHOST_HOSTNAME}:{self.port} x",
        )
        with patch.object(SALTMDBHandler, "get_db_connection", side_effect=AssertionError):
            with patch.object(SALTMDBHandler, "send_static_asset", side_effect=AssertionError):
                for host in bad_hosts:
                    with self.subTest(host=host):
                        status, body = self._request(host=host, path=API_PATH)
                        self.assertEqual(status, MISDIRECTED_STATUS)
                        if host:
                            self.assertNotIn(host.encode(), body)

    def test_non_ascii_port_is_rejected(self):
        arabic_port = "".join(chr(ord("٠") + int(digit)) for digit in str(self.port))
        response = self._raw_request(
            f"GET {API_PATH} HTTP/1.1\r\nHost: {LOCALHOST_HOSTNAME}:{arabic_port}\r\n\r\n".encode()
        )
        self.assertIn(MISDIRECTED_STATUS_LINE, response.splitlines()[0])

    def test_rejected_body_does_not_echo_host(self):
        sent_host = f"rebind-probe.example:{self.port}"
        status, body = self._request(path=API_PATH, host=sent_host)
        self.assertEqual(status, MISDIRECTED_STATUS)
        self.assertNotIn(sent_host.encode(), body)

    def test_expect_100_rejects_before_interim_response(self):
        with patch.object(SALTMDBHandler, "protocol_version", "HTTP/1.1"):
            response = self._raw_request(
                (
                    f"POST {API_PATH} HTTP/1.1\r\n"
                    f"Host: {BAD_HOSTNAME}:{self.port}\r\n"
                    "Expect: 100-continue\r\n"
                    "Content-Length: 0\r\n\r\n"
                ).encode()
            )
        self.assertIn(MISDIRECTED_STATUS_LINE, response.splitlines()[0])

    def test_outer_host_whitespace_is_accepted(self):
        status, _ = self._request(host=f"  {LOCALHOST_HOSTNAME}:{self.port} ")
        self.assertEqual(status, 200)

    def test_non_ows_control_whitespace_is_rejected(self):
        for whitespace in ("\x0b", "\x0c"):
            with self.subTest(whitespace=repr(whitespace)):
                response = self._raw_request(
                    (
                        f"GET {API_PATH} HTTP/1.1\r\n"
                        f"Host: {whitespace}{LOCALHOST_HOSTNAME}:{self.port}{whitespace}\r\n\r\n"
                    ).encode()
                )
                self.assertIn(MISDIRECTED_STATUS_LINE, response.splitlines()[0])

    def test_parent_parse_errors_are_not_reclassified_as_421(self):
        with patch.object(SALTMDBHandler, "get_db_connection", side_effect=AssertionError):
            with patch.object(SALTMDBHandler, "send_static_asset", side_effect=AssertionError):
                response = self._raw_request(b"BROKEN\r\n\r\n")
        self.assertIn(b"Error code: 400", response)
        self.assertEqual(response.count(b"Error code: 400"), 1)
        self.assertNotIn(b"421", response)

    def test_oversized_header_preserves_parent_431(self):
        oversized_header = b"X-Fill: " + b"a" * 65536
        request = (
            b"GET / HTTP/1.1\r\nHost: localhost:"
            + str(self.port).encode()
            + b"\r\n"
            + oversized_header
            + b"\r\n\r\n"
        )
        with patch.object(SALTMDBHandler, "get_db_connection", side_effect=AssertionError):
            with patch.object(SALTMDBHandler, "send_static_asset", side_effect=AssertionError):
                response = self._raw_request(request)
        self.assertIn(b" 431 ", response.splitlines()[0])
        self.assertEqual(response.count(b"HTTP/1.0 431"), 1)

    def test_all_methods_reject_bad_hosts(self):
        bad_host = f"{BAD_HOSTNAME}:{self.port}"
        for method in ("GET", "HEAD", "POST", "OPTIONS"):
            with self.subTest(method=method):
                with patch.object(SALTMDBHandler, "send_json") as send_json:
                    status, _ = self._request(method=method, path=API_PATH, host=bad_host)
                    self.assertEqual(status, MISDIRECTED_STATUS)
                    send_json.assert_not_called()

    def test_unknown_method_rejects_bad_host_before_501(self):
        response = self._raw_request(
            f"PATCH {API_PATH} HTTP/1.1\r\nHost: {BAD_HOSTNAME}:{self.port}\r\n\r\n".encode()
        )
        self.assertIn(MISDIRECTED_STATUS_LINE, response.splitlines()[0])

    def test_duplicate_host_headers_are_rejected_in_both_orders(self):
        for first, second in (
            (f"{LOCALHOST_HOSTNAME}:{self.port}", f"{BAD_HOSTNAME}:{self.port}"),
            (f"{BAD_HOSTNAME}:{self.port}", f"{LOCALHOST_HOSTNAME}:{self.port}"),
        ):
            with self.subTest(first=first, second=second):
                response = self._raw_request(
                    (f"GET {API_PATH} HTTP/1.1\r\nHost: {first}\r\nHost: {second}\r\n\r\n").encode()
                )
                self.assertIn(MISDIRECTED_STATUS_LINE, response.splitlines()[0])

    def test_missing_host_header_is_rejected(self):
        response = self._raw_request(b"GET / HTTP/1.0\r\n\r\n")
        self.assertIn(MISDIRECTED_STATUS_LINE, response.splitlines()[0])

    def test_raw_http_09_does_not_reach_routes(self):
        with patch.object(SALTMDBHandler, "get_db_connection", side_effect=AssertionError):
            with patch.object(SALTMDBHandler, "send_static_asset", side_effect=AssertionError):
                self._raw_request(b"GET /api/stats\r\n\r\n")

    def test_rejected_requests_do_not_reach_post_or_static_routes(self):
        with patch.object(SALTMDBHandler, "send_json") as send_json:
            with patch.object(SALTMDBHandler, "send_static_asset") as send_static_asset:
                self._request(method="POST", path=API_PATH, host=f"{ALT_BAD_HOSTNAME}:{self.port}")
                self._request(path="/static/viewer.js", host=f"{ALT_BAD_HOSTNAME}:{self.port}")
        send_json.assert_not_called()
        send_static_asset.assert_not_called()


class HostValidationUnitTests(unittest.TestCase):
    @staticmethod
    def _handler(server, host):
        handler = SALTMDBHandler.__new__(SALTMDBHandler)
        handler.server = server
        headers = Message()
        if host is not None:
            headers["Host"] = host
        handler.headers = headers
        return handler

    def test_host_without_server_address_is_denied(self):
        handler = self._handler(SimpleNamespace(), f"{LOCALHOST_HOSTNAME}:8080")
        self.assertFalse(handler._host_allowed())

    def test_non_tuple_server_address_is_denied(self):
        handler = self._handler(
            SimpleNamespace(server_address=LOOPBACK_HOST), f"{LOCALHOST_HOSTNAME}:8080"
        )
        self.assertFalse(handler._host_allowed())

    def test_short_server_addresses_are_denied(self):
        for address in ((), (LOOPBACK_HOST,)):
            with self.subTest(address=address):
                handler = self._handler(
                    SimpleNamespace(server_address=address), f"{LOCALHOST_HOSTNAME}:8080"
                )
                self.assertFalse(handler._host_allowed())

    def test_handle_expect_100_rejects_before_continue(self):
        handler = self._handler(SimpleNamespace(server_address=(LOOPBACK_HOST, 8080)), "bad:8080")
        handler.send_error = Mock()
        self.assertFalse(handler.handle_expect_100())
        handler.send_error.assert_called_once_with(MISDIRECTED_STATUS, MISDIRECTED_MESSAGE)


if __name__ == "__main__":
    unittest.main()
