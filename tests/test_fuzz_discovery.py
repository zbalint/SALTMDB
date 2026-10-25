import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from hypothesis import example, given, strategies as st

from saltmdb.daemon import discovery


KEY = "fuzz-input-edges"


discovery_body = st.one_of(
    st.integers(min_value=2000, max_value=200000).map(lambda depth: b"[" * depth + b"]" * depth),
    st.integers(min_value=4290, max_value=6000).map(
        lambda digits: b"{" + b'"service_port":' + b"9" * digits + b"}"
    ),
    st.binary(max_size=256),
    st.just(
        json.dumps(
            {"service_port": 4000, "auth_token": "fuzz-token"}, separators=(",", ":")
        ).encode()
    ),
)


@unittest.skipIf(sys.platform == "win32", "discovery ownership checks differ on Windows")
class TestFuzzDiscovery(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.home_patch = patch.dict(
            os.environ, {"HOME": self.temp_dir.name, "USERPROFILE": self.temp_dir.name}
        )
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    @given(discovery_body)
    @example(b"[" * 200000 + b"]" * 200000)
    def test_s1_hostile_discovery_file_is_none_or_dict(self, body: bytes):
        path = discovery.discovery_path(KEY)
        with open(path, "wb") as file:
            file.write(body)
        os.chmod(path, 0o600)
        result = discovery.read(KEY)
        self.assertTrue(result is None or isinstance(result, dict))


if __name__ == "__main__":
    unittest.main()
