import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

from saltmdb.mcp import copilot_session


class TestCopilotSessionFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _publish(self, **env):
        with mock.patch.dict(os.environ, env, clear=False):
            return copilot_session.publish("copilot", "sess-1", "/db/path", directory=self.dir)

    def test_keyed_by_copilot_loader_pid_when_present(self):
        path = self._publish(COPILOT_LOADER_PID="4242")
        self.assertEqual(os.path.basename(path), "copilot_adapter_4242.json")

    def test_falls_back_to_parent_pid(self):
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("COPILOT_LOADER_PID", None)
            path = copilot_session.publish("copilot", "sess-1", "/db/path", directory=self.dir)
        self.assertEqual(os.path.basename(path), f"copilot_adapter_{os.getppid()}.json")

    def test_payload_carries_identity_and_owner_pid(self):
        path = self._publish(COPILOT_LOADER_PID="4242")
        data = json.load(open(path, encoding="utf-8"))
        self.assertEqual(data["agent_id"], "copilot")
        self.assertEqual(data["agent_session_id"], "sess-1")
        self.assertEqual(data["db_path"], "/db/path")
        self.assertEqual(data["adapter_pid"], os.getpid())

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits")
    def test_file_is_owner_only(self):
        path = self._publish(COPILOT_LOADER_PID="4242")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_non_numeric_loader_pid_is_not_used_as_a_path_component(self):
        path = self._publish(COPILOT_LOADER_PID="../evil")
        self.assertEqual(os.path.dirname(path), self.dir)
        self.assertEqual(os.path.basename(path), f"copilot_adapter_{os.getppid()}.json")

    def test_remove_deletes_and_tolerates_missing(self):
        path = self._publish(COPILOT_LOADER_PID="4242")
        copilot_session.remove(path)
        self.assertFalse(os.path.exists(path))
        copilot_session.remove(path)  # already gone: no error

    def test_publish_sweeps_files_of_dead_adapters(self):
        stale = os.path.join(self.dir, "copilot_adapter_1.json")
        with open(stale, "w", encoding="utf-8") as f:
            json.dump({"adapter_pid": 999999}, f)
        with mock.patch.object(copilot_session, "_pid_alive", lambda pid: pid != 999999):
            self._publish(COPILOT_LOADER_PID="4242")
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "copilot_adapter_4242.json")))

    def test_sweep_keeps_unreadable_files(self):
        odd = os.path.join(self.dir, "copilot_adapter_7.json")
        with open(odd, "w", encoding="utf-8") as f:
            f.write("not json")
        self._publish(COPILOT_LOADER_PID="4242")
        self.assertTrue(os.path.exists(odd))


if __name__ == "__main__":
    unittest.main()


class TestLifespanPublishing(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from saltmdb.mcp.identity import SESSION_IDENTITY

        self.identity = SESSION_IDENTITY
        self.identity.reset()
        self._env = mock.patch.dict(os.environ, {"SALTMDB_AGENT_ID": "copilot"}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.identity.reset()

    async def _run(self, enabled: bool):
        from saltmdb.mcp.server import server_lifespan

        with (
            mock.patch("saltmdb.mcp.server.SessionConnection"),
            mock.patch("saltmdb.mcp.server.get_db_path", return_value="/db/x"),
            mock.patch(
                "saltmdb.daemon.client.reachable_daemon_info",
                return_value={"db_path": "/db/x"},
            ),
            mock.patch("saltmdb.mcp.server.is_trace_capture_enabled", return_value=enabled),
            mock.patch("saltmdb.mcp.server.copilot_session") as cs,
        ):
            cs.publish.return_value = "/pub/path"
            async with server_lifespan(mock.MagicMock()):
                pass
            return cs

    async def test_publishes_when_capture_enabled_and_removes_on_exit(self):
        cs = await self._run(True)
        cs.publish.assert_called_once_with("copilot", self.identity.agent_session_id, "/db/x")
        cs.remove.assert_called_once_with("/pub/path")

    async def test_does_not_publish_when_capture_disabled(self):
        cs = await self._run(False)
        cs.publish.assert_not_called()
        cs.remove.assert_not_called()
