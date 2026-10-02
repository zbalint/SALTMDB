import hashlib
import importlib.util
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "trace_capture_copilot", HOOKS_DIR / "saltmdb-stop-trace-capture.py"
)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

IA = "interaction-a"
IB = "interaction-b"


def ev(type_, **data):
    return json.dumps({"type": type_, "data": data})


def user(content, interaction=IA, message_id="m1"):
    return ev(
        "user.message",
        content=content,
        transformedContent="<dt>" + content,
        messageId=message_id,
        interactionId=interaction,
    )


def assistant(content="", interaction=IA, requests=None, origin="m1"):
    return ev(
        "assistant.message",
        content=content,
        toolRequests=requests or [],
        interactionId=interaction,
        originatingMessageId=origin,
    )


def tool(name, call_id, result, interaction=IA, success=True, server="saltmdb"):
    start = ev(
        "tool.execution_start",
        toolCallId=call_id,
        toolName=f"{server}-{name}",
        mcpServerName=server,
        mcpToolName=name,
        arguments={},
    )
    done = ev(
        "tool.execution_complete",
        toolCallId=call_id,
        interactionId=interaction,
        success=success,
        result={"content": result},
    )
    return [start, done]


class TestParseInteractions(unittest.TestCase):
    def test_prompt_and_final_reply_of_a_tool_loop(self):
        lines = [
            user("do it"),
            assistant("", requests=[{"toolCallId": "c1"}]),
            *tool("search_memory", "c1", "{}"),
            assistant("all done"),
        ]
        (it,) = hook.parse_interactions(lines)
        self.assertEqual(it["interaction_id"], IA)
        self.assertEqual(it["messages"], ["do it"])
        self.assertEqual(it["final"], "all done")
        self.assertEqual(it["writes"], [])

    def test_text_before_a_tool_call_is_not_the_final_reply(self):
        lines = [user("go"), assistant("let me look", requests=[{"toolCallId": "c1"}])]
        (it,) = hook.parse_interactions(lines)
        self.assertIsNone(it["final"])

    def test_interrupted_interaction_has_no_final(self):
        (it,) = hook.parse_interactions([user("go"), assistant("")])
        self.assertIsNone(it["final"])

    def test_mid_turn_message_is_a_second_message_of_the_same_interaction(self):
        lines = [
            user("first", message_id="m1"),
            user("wait, also this", message_id="m2"),
            assistant("ok"),
        ]
        (it,) = hook.parse_interactions(lines)
        self.assertEqual(it["messages"], ["first", "wait, also this"])

    def test_two_interactions_keep_event_order(self):
        lines = [
            user("one", IA, "m1"),
            assistant("r1", IA, origin="m1"),
            user("two", IB, "m2"),
            assistant("r2", IB, origin="m2"),
        ]
        its = hook.parse_interactions(lines)
        self.assertEqual([i["interaction_id"] for i in its], [IA, IB])
        self.assertEqual([i["final"] for i in its], ["r1", "r2"])

    def test_memory_writes_are_collected_from_successful_calls_only(self):
        lines = [
            user("store things"),
            *tool("store_memory", "c1", json.dumps({"status": "ok", "data": {"id": "e-new"}})),
            *tool("revise_memory", "c2", json.dumps({"status": "ok", "data": {"new_id": "e-rev"}})),
            *tool(
                "supersede_memory", "c3", json.dumps({"status": "ok", "data": {"new_id": "e-sup"}})
            ),
            *tool(
                "consolidate_memories",
                "c4",
                json.dumps({"status": "ok", "data": {"entity_id": "e-con"}}),
            ),
            *tool("store_memory", "c5", json.dumps({"status": "rejected", "errors": []})),
            *tool("store_memory", "c6", "boom", success=False),
            *tool("search_memory", "c7", json.dumps({"id": "not-a-write"})),
            assistant("done"),
        ]
        (it,) = hook.parse_interactions(lines)
        self.assertEqual(
            it["writes"],
            [
                ("store_memory", "e-new"),
                ("revise_memory", "e-rev"),
                ("supersede_memory", "e-sup"),
                ("consolidate_memories", "e-con"),
            ],
        )

    def test_garbage_lines_are_skipped(self):
        (it,) = hook.parse_interactions(["", "not json", "[1]", user("hi"), assistant("yo")])
        self.assertEqual(it["final"], "yo")


class _FakeDaemon:
    def __init__(self, token="tok"):
        self.token, self.calls = token, []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _recv(self, conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            try:
                (n,) = struct.unpack(">I", self._recv(conn, 4))
                req = json.loads(self._recv(conn, n))
                self.calls.append(req)
                body = json.dumps(
                    {
                        "id": req["id"],
                        "ok": True,
                        "result": {"status": "ok", "data": {}, "warnings": []},
                    }
                ).encode()
                conn.sendall(struct.pack(">I", len(body)) + body)
            except Exception:
                pass
            finally:
                conn.close()

    def close(self):
        self.srv.close()


class TestRun(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / ".saltmdb").mkdir()
        self.db_path = str(self.home / "x.db")
        self.transcript = self.home / "events.jsonl"
        self.lines = [
            user("hello"),
            assistant("", requests=[{"toolCallId": "c1"}]),
            *tool("store_memory", "c1", json.dumps({"status": "ok", "data": {"id": "E1"}})),
            assistant("stored"),
        ]
        self.transcript.write_text("\n".join(self.lines), encoding="utf-8")
        self.daemon = _FakeDaemon()
        key = hashlib.sha256(os.path.realpath(self.db_path).encode()).hexdigest()[:16]
        (self.home / ".saltmdb" / f"daemon_{key}.json").write_text(
            json.dumps({"service_port": self.daemon.port, "auth_token": "tok"}), encoding="utf-8"
        )

    def tearDown(self):
        self.daemon.close()
        import shutil

        shutil.rmtree(self.home, ignore_errors=True)

    def _adapter(self, key):
        (self.home / ".saltmdb" / f"copilot_adapter_{key}.json").write_text(
            json.dumps(
                {
                    "agent_id": "copilot",
                    "agent_session_id": "adapter-1",
                    "db_path": self.db_path,
                    "adapter_pid": 1,
                }
            ),
            encoding="utf-8",
        )

    def _run(self, payload=None, environ=None, ppid=777):
        payload = payload or {"sessionId": "cs-1", "transcriptPath": str(self.transcript)}
        return hook.run(payload, home=self.home, environ=environ or {}, ppid=ppid)

    def test_captures_start_link_complete_in_order_with_adapter_identity(self):
        self._adapter(777)
        self._run()
        calls = [(c["params"]["tool"], c["params"]["kwargs"]) for c in self.daemon.calls]
        self.assertEqual(
            [t for t, _ in calls],
            ["capture_trace_start", "capture_trace_memory_link", "capture_trace_complete"],
        )
        self.assertTrue(all(c["token"] == "tok" for c in self.daemon.calls))
        start, link, complete = (k for _, k in calls)
        for kw in (start, link, complete):
            self.assertEqual((kw["agent_id"], kw["agent_session_id"]), ("copilot", "adapter-1"))
            self.assertEqual(kw["harness_turn_id"], IA)
        self.assertEqual(
            (start["harness"], start["harness_session_id"], start["user_prompt"]),
            ("copilot", "cs-1", "hello"),
        )
        self.assertEqual((link["entity_id"], link["just_run_tool_name"]), ("E1", "store_memory"))
        self.assertEqual(complete["final_assistant_message"], "stored")

    def test_loader_pid_takes_precedence_then_parent_pid(self):
        self._adapter(555)
        self._run(environ={"COPILOT_LOADER_PID": "555"}, ppid=999)
        self.assertEqual(len(self.daemon.calls), 3)

    def test_no_adapter_file_means_capture_is_off_and_nothing_is_sent(self):
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.daemon.calls, [])

    def test_session_end_payload_without_transcript_path_uses_copilot_session_state(self):
        self._adapter(777)
        state = self.home / ".copilot" / "session-state" / "cs-1"
        state.mkdir(parents=True)
        (state / "events.jsonl").write_text(self.transcript.read_text(), encoding="utf-8")
        self._run(payload={"sessionId": "cs-1", "reason": "user_exit"})
        self.assertEqual(len(self.daemon.calls), 3)

    def test_interrupted_interaction_is_started_but_never_completed(self):
        self._adapter(777)
        self.transcript.write_text("\n".join([user("hello"), assistant("")]), encoding="utf-8")
        self._run()
        self.assertEqual([c["params"]["tool"] for c in self.daemon.calls], ["capture_trace_start"])

    def test_mid_turn_message_is_sent_as_a_second_start(self):
        self._adapter(777)
        self.transcript.write_text(
            "\n".join([user("a", message_id="m1"), user("b", message_id="m2"), assistant("ok")]),
            encoding="utf-8",
        )
        self._run()
        prompts = [
            c["params"]["kwargs"]["user_prompt"]
            for c in self.daemon.calls
            if c["params"]["tool"] == "capture_trace_start"
        ]
        self.assertEqual(prompts, ["a", "b"])

    def test_unreachable_daemon_is_logged_not_raised(self):
        self._adapter(777)
        self.daemon.close()
        self.assertEqual(self._run(), 0)
        log = (self.home / ".saltmdb" / "hooks" / "trace-capture.log").read_text()
        self.assertIn("cs-1", log)

    def test_missing_transcript_is_logged_not_raised(self):
        self._adapter(777)
        self.assertEqual(
            self._run({"sessionId": "cs-1", "transcriptPath": str(self.home / "nope")}), 0
        )
        self.assertEqual(self.daemon.calls, [])


class TestExampleRegistration(unittest.TestCase):
    def test_copilot_example_registers_capture_on_agent_stop_and_session_end(self):
        hooks = json.loads((HOOKS_DIR / "copilot-hooks-example.json").read_text())["hooks"]
        for event in ("agentStop", "sessionEnd"):
            entries = [h for h in hooks[event] if "saltmdb-stop-trace-capture.py" in h["bash"]]
            self.assertEqual(len(entries), 1, event)
            self.assertIn("saltmdb-stop-trace-capture.py", entries[0]["powershell"])
            self.assertEqual(entries[0]["type"], "command")


if __name__ == "__main__":
    unittest.main()
