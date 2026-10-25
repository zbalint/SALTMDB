import re
import unittest
from email.message import Message
from types import SimpleNamespace

from hypothesis import example, given, strategies as st

from saltmdb.viewer.routes import SALTMDBHandler


host_piece = st.sampled_from(
    (
        "localhost",
        "LocalHost",
        "127.0.0.1",
        "[::1]",
        "[::1",
        "::1",
        "8080",
        "08080",
        "8081",
        "٨٠٨٠",
        ":",
        " ",
        "\t",
        "\r",
        "\n",
        "\x0b",
        "\x00",
        "@",
        "evil.example",
        "localhost.",
        "[",
        "]",
        "%",
    )
)
host_values = st.lists(host_piece, min_size=1, max_size=8).map("".join)


class TestFuzzViewerHost(unittest.TestCase):
    @staticmethod
    def _handler(host: str | None, *extra_hosts: str):
        handler = SALTMDBHandler.__new__(SALTMDBHandler)
        handler.server = SimpleNamespace(server_address=("127.0.0.1", 8080))
        headers = Message()
        if host is not None:
            headers["Host"] = host
        for extra_host in extra_hosts:
            headers["Host"] = extra_host
        handler.headers = headers
        return handler

    @staticmethod
    def _oracle(value: str) -> bool:
        return bool(
            re.fullmatch(
                r"(localhost|127\.0\.0\.1|\[::1\]):8080",
                value.strip(" \t"),
                re.IGNORECASE | re.ASCII,
            )
        )

    @given(host_values)
    @example("localhost:8080")
    @example("LocalHost:8080")
    @example("127.0.0.1:8080")
    @example("[::1]:8080")
    @example("localhost:08080")
    @example("localhost:٨٠٨٠")
    @example(" localhost:8080\t")
    @example("localhost:8080\n")
    def test_h1_host_check_matches_ascii_oracle(self, value: str):
        self.assertEqual(self._handler(value)._host_allowed(), self._oracle(value))

    def test_h1_zero_or_duplicate_host_headers_are_denied(self):
        self.assertFalse(self._handler(None)._host_allowed())
        self.assertFalse(self._handler("localhost:8080", "localhost:8080")._host_allowed())


if __name__ == "__main__":
    unittest.main()
