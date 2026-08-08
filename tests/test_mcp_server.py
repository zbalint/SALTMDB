import os
import tempfile
import shutil
import unittest
from unittest.mock import MagicMock, patch

from saltmdb.mcp.server import server_lifespan


class TestMCPServerLifespan(unittest.IsolatedAsyncioTestCase):
    """Track B (scratch/plans/track_b_daemon_detailed.md §5/§8): server_lifespan now owns only
    the SessionConnection's open/close -- session ref-counting/grace-period logic moved to the
    daemon's own _DaemonState (see tests/test_daemon_discovery.py and friends for that)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_lifespan_opens_and_closes_one_session_connection(self):
        mock_session = MagicMock()
        with (
            patch("saltmdb.mcp.server.get_db_path", return_value=self.db_path),
            patch("saltmdb.mcp.server.SessionConnection", return_value=mock_session) as mock_cls,
        ):
            async with server_lifespan(MagicMock()):
                mock_cls.assert_called_once_with(self.db_path)
                mock_session.open.assert_called_once()
                mock_session.close.assert_not_called()
            mock_session.close.assert_called_once()

    async def test_lifespan_propagates_body_exceptions_but_still_closes_session(self):
        """Codex round-1 finding on the original shape (server_lifespan not wrapping the yield in
        try/finally, silently leaking session.close() on any body exception): the exception must
        still propagate to the caller, but session.close() must now run regardless via finally."""
        mock_session = MagicMock()
        with (
            patch("saltmdb.mcp.server.get_db_path", return_value=self.db_path),
            patch("saltmdb.mcp.server.SessionConnection", return_value=mock_session),
        ):
            with self.assertRaises(RuntimeError):
                async with server_lifespan(MagicMock()):
                    raise RuntimeError("boom")
            mock_session.open.assert_called_once()
            mock_session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
