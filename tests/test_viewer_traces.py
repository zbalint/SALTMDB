import os
import shutil
import tempfile
import unittest
from pathlib import Path

from saltmdb.db.schema import init_db
from saltmdb.viewer.routes import SALTMDBHandler
from saltmdb.viewer.templates import get_frontend_html


class DummyRequest:
    def makefile(self, *args, **kwargs):
        import io

        return io.BytesIO(b"")


class DummyServer:
    pass


class TestViewerTraces(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "traces.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SALTMDB_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _get(self, method_name, *args):
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())
        captured = {}
        handler.send_json = lambda data, status=200: captured.update(data=data, status=status)
        getattr(handler, method_name)(*args)
        return captured["status"], captured["data"]

    def _insert_trace(
        self,
        trace_id,
        session_id,
        created_at,
        prompt="hello",
        final=None,
        status="completed",
    ):
        self.conn.execute(
            """INSERT INTO conversation_traces
               (id, agent_session_id, owner_id, harness, harness_session_id, harness_turn_id,
                status, user_prompt, user_prompt_hash, final_assistant_message,
                final_assistant_message_hash, created_at, updated_at, completed_at)
               VALUES (?, ?, 'claude', 'claude_code', 'hs', ?, ?, ?, 'h', ?, 'h', ?, ?, ?)""",
            (
                trace_id,
                session_id,
                f"turn-{trace_id}",
                status,
                prompt,
                final,
                created_at,
                created_at,
                created_at if final is not None else None,
            ),
        )
        self.conn.commit()

    def test_list_traces_for_session_returns_newest_first_previews_with_total(self):
        long_prompt = "p" * 500
        self._insert_trace("t1", "sess-a", "2026-09-28T10:00:00+00:00", prompt="first", final="one")
        self._insert_trace(
            "t2", "sess-a", "2026-09-28T11:00:00+00:00", prompt=long_prompt, final="f" * 500
        )
        self._insert_trace("t3", "sess-b", "2026-09-28T12:00:00+00:00")

        status, data = self._get("get_traces", {"agent_session_id": ["sess-a"]})

        self.assertEqual(status, 200)
        self.assertEqual(data["total_count"], 2)
        self.assertEqual([t["trace_id"] for t in data["traces"]], ["t2", "t1"])
        newest = data["traces"][0]
        self.assertEqual(newest["harness"], "claude_code")
        self.assertEqual(newest["status"], "completed")
        self.assertEqual(newest["agent_session_id"], "sess-a")
        self.assertLessEqual(len(newest["user_prompt_snippet"]), 200)
        self.assertLessEqual(len(newest["final_assistant_message_snippet"]), 200)
        self.assertNotIn("user_prompt", newest)
        self.assertNotIn("final_assistant_message", newest)

    def _insert_entity_and_link(self, trace_id, entity_id, title, operation="store_memory_new"):
        self.conn.execute(
            """INSERT INTO entities (id, created_at, updated_at, last_accessed_at, title, full_content)
               VALUES (?, '2026-09-28T10:00:00+00:00', '2026-09-28T10:00:00+00:00',
                       '2026-09-28T10:00:00+00:00', ?, 'body')""",
            (entity_id, title),
        )
        self.conn.execute(
            """INSERT INTO trace_memory_links (id, trace_id, entity_id, content_hash,
                                               write_operation, created_at)
               VALUES (?, ?, ?, 'ch', ?, '2026-09-28T10:01:00+00:00')""",
            (f"link-{entity_id}", trace_id, entity_id, operation),
        )
        self.conn.commit()

    def test_trace_detail_returns_full_text_and_linked_memories(self):
        full_prompt = "q" * 500
        full_final = "a" * 500
        self._insert_trace(
            "t1", "sess-a", "2026-09-28T10:00:00+00:00", prompt=full_prompt, final=full_final
        )
        self._insert_entity_and_link("t1", "mem-1", "Stored thing", "revise_memory")

        status, data = self._get("get_trace_detail", "t1")

        self.assertEqual(status, 200)
        self.assertEqual(data["trace_id"], "t1")
        self.assertEqual(data["user_prompt"], full_prompt)
        self.assertEqual(data["final_assistant_message"], full_final)
        self.assertEqual(data["agent_session_id"], "sess-a")
        self.assertEqual(
            data["linked_memories"],
            [{"entity_id": "mem-1", "title": "Stored thing", "write_operation": "revise_memory"}],
        )

    def test_trace_detail_unknown_id_is_404(self):
        status, data = self._get("get_trace_detail", "nope")

        self.assertEqual(status, 404)
        self.assertIn("error", data)

    def test_do_get_dispatches_trace_routes(self):
        self._insert_trace("t1", "sess-a", "2026-09-28T10:00:00+00:00", final="x")
        for path, key in (
            ("/api/traces?agent_session_id=sess-a", "traces"),
            ("/api/traces/t1", "user_prompt"),
        ):
            handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())
            captured = {}
            handler.send_json = lambda data, status=200, c=captured: c.update(
                data=data, status=status
            )
            handler.path = path
            handler.do_GET()
            self.assertEqual(captured["status"], 200, path)
            self.assertIn(key, captured["data"], path)

    def test_list_traces_filters_by_linked_memory(self):
        self._insert_trace("t1", "sess-a", "2026-09-28T10:00:00+00:00", final="x")
        self._insert_trace("t2", "sess-a", "2026-09-28T11:00:00+00:00", final="y")
        self._insert_entity_and_link("t2", "mem-1", "Stored thing")

        status, data = self._get("get_traces", {"entity_id": ["mem-1"]})

        self.assertEqual(status, 200)
        self.assertEqual(data["total_count"], 1)
        self.assertEqual([t["trace_id"] for t in data["traces"]], ["t2"])

    def test_list_traces_rejects_out_of_range_limit(self):
        status, data = self._get("get_traces", {"limit": ["1000"]})

        self.assertEqual(status, 400)
        self.assertIn("limit", data["error"])


class TestViewerTraceFrontendContracts(unittest.TestCase):
    """Static-source contracts: no browser is available, so these pin the wiring, not behavior."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1] / "src/saltmdb/viewer/static"
        cls.script = (root / "viewer.js").read_text(encoding="utf-8")
        cls.stylesheet = (root / "viewer.css").read_text(encoding="utf-8")
        cls.shell = get_frontend_html()

    def test_shell_has_a_labelled_trace_dialog(self):
        self.assertIn('<dialog id="trace-detail" aria-labelledby="trace-detail-title">', self.shell)
        self.assertIn('id="trace-detail-content"', self.shell)
        self.assertIn(
            'id="close-trace-detail" aria-label="Close conversation trace detail"', self.shell
        )

    def test_trace_dialog_fetches_detail_and_renders_untrusted_text_as_text(self):
        self.assertIn("const openTraceDetail = async (traceId", self.script)
        self.assertIn("/api/traces/${encodeURIComponent(traceId)}", self.script)
        self.assertIn("node('pre', data.user_prompt || '—', 'trace-text')", self.script)
        self.assertIn("node('pre', data.final_assistant_message || '—', 'trace-text')", self.script)
        self.assertIn("traceDialog.showModal()", self.script)
        self.assertIn("document.querySelector('#close-trace-detail')", self.script)
        self.assertIn("traceDialog._invoker", self.script)
        # Trace text is untrusted history: it must never reach an HTML sink.
        trace_block = self.script[self.script.index("const openTraceDetail") :]
        trace_block = trace_block[: trace_block.index("const overview")]
        self.assertNotIn("innerHTML", trace_block)

    def test_memory_dialog_lists_provenance_traces_and_opens_them(self):
        self.assertIn("section('Conversation traces'", self.script)
        self.assertIn("(data.trace_provenance || [])", self.script)
        self.assertIn("openTraceDetail(trace.trace_id, click.currentTarget)", self.script)
        self.assertIn("No conversation trace is linked to this memory.", self.script)

    def test_sessions_list_shows_trace_counts(self):
        self.assertIn("'Memories', 'Events', 'Traces'", self.script)
        self.assertIn("String(item.trace_count ?? 0)", self.script)

    def test_session_detail_lists_its_traces(self):
        self.assertIn("/api/traces?agent_session_id=${encodeURIComponent(sessionId)}", self.script)
        self.assertIn("['Traces', String(sessionData.trace_count ?? 0)]", self.script)
        self.assertIn("section(`${tracesData.total_count} conversation traces`", self.script)
        self.assertIn("trace.user_prompt_snippet", self.script)
        self.assertIn("No conversation traces were captured for this session.", self.script)

    def test_long_trace_text_scrolls_inside_its_block(self):
        self.assertRegex(self.stylesheet, r"\.trace-text\s*\{[^}]*max-height:[^}]*overflow:\s*auto")


if __name__ == "__main__":
    unittest.main()
