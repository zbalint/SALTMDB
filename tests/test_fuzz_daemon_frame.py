import json
import socket
import unittest

from hypothesis import example, given, strategies as st

from saltmdb.config import DAEMON_RPC_MAX_MESSAGE_BYTES
from saltmdb.daemon.protocol import FrameError, recv_frame, send_frame


class FakeSocket:
    def __init__(self, data: bytes):
        self._data = bytearray(data)

    def recv(self, size: int) -> bytes:
        chunk = bytes(self._data[:size])
        del self._data[:size]
        return chunk

    @property
    def unread(self) -> bytes:
        return bytes(self._data)


json_leaf = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=8),
)
json_value = st.recursive(
    json_leaf,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=8), children, max_size=4),
    ),
    max_leaves=30,
)
json_object = st.dictionaries(st.text(max_size=8), json_value, max_size=6)
wire_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=8)
wire_value = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        wire_text,
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(wire_text, children, max_size=4),
    ),
    max_leaves=30,
)
wire_object = st.dictionaries(wire_text, wire_value, max_size=6)


class TestDaemonFrameFuzz(unittest.TestCase):
    @given(st.binary(max_size=256))
    def test_f1_random_stream_is_dict_or_frame_error(self, stream: bytes):
        try:
            result = recv_frame(FakeSocket(stream))
        except FrameError:
            return
        self.assertIsInstance(result, dict)

    @given(
        st.one_of(
            st.recursive(
                st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=8)),
                lambda children: st.one_of(
                    st.lists(children, max_size=4),
                    st.dictionaries(st.text(max_size=8), children, max_size=4),
                ),
                max_leaves=30,
            ).map(lambda value: json.dumps(value).encode("utf-8")),
            st.integers(min_value=2000, max_value=400000).map(
                lambda depth: b'{"a":' + b"[" * depth + b"]" * depth + b"}"
            ),
            st.integers(min_value=4290, max_value=6000).map(
                lambda digits: b'{"a":' + b"9" * digits + b"}"
            ),
            st.sampled_from([b"[]", b"1", b"null", b'"x"']),
            st.binary(max_size=256),
        )
    )
    @example(b'{"a":' + b"[" * 200000 + b"]" * 200000 + b"}")
    @example(b'{"a":' + b"9" * 5000 + b"}")
    def test_f2_hostile_body_is_dict_or_frame_error(self, body: bytes):
        stream = len(body).to_bytes(4, "big") + body
        try:
            result = recv_frame(FakeSocket(stream))
        except FrameError:
            return
        self.assertIsInstance(result, dict)

    @given(json_object)
    def test_f3_send_recv_round_trip(self, obj: dict):
        left, right = socket.socketpair()
        left.settimeout(5)
        right.settimeout(5)
        try:
            send_frame(left, obj)
            self.assertEqual(recv_frame(right), obj)
        finally:
            left.close()
            right.close()

    @given(st.integers(min_value=DAEMON_RPC_MAX_MESSAGE_BYTES + 1, max_value=2**32 - 1))
    def test_f4_oversize_length_rejects_before_body_read(self, length: int):
        fake = FakeSocket(length.to_bytes(4, "big") + b"x" * 16)
        with self.assertRaisesRegex(FrameError, "exceeds"):
            recv_frame(fake)
        self.assertEqual(fake.unread, b"x" * 16)

    def test_f4_boundary_length_is_not_oversize(self):
        fake = FakeSocket(DAEMON_RPC_MAX_MESSAGE_BYTES.to_bytes(4, "big") + b"x" * 16)
        with self.assertRaisesRegex(FrameError, "connection closed") as raised:
            recv_frame(fake)
        self.assertNotIn("exceeds", str(raised.exception))

    @given(wire_object)
    @example({"é": "😀"})
    def test_f5_raw_utf8_frame_round_trip(self, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        stream = len(body).to_bytes(4, "big") + body
        self.assertEqual(recv_frame(FakeSocket(stream)), obj)
