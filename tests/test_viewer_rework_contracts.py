import os
import re
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from saltmdb.daemon.server import _DaemonState
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.viewer.context import ViewerReadGateway
from saltmdb.viewer.routes import SALTMDBHandler, STATIC_ASSETS
from saltmdb.viewer.templates import get_frontend_html


class _Request:
    headers = {}

    def makefile(self, *args, **kwargs):
        import io

        return io.BytesIO(b"")


class _Server:
    pass


class TestViewerReworkContracts(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "viewer.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SALTMDB_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _handler(self, daemon_state=None):
        server = _Server()
        server.viewer_gateway = ViewerReadGateway(self.db_path, daemon_state)
        server.daemon_state = daemon_state
        return SALTMDBHandler(_Request(), ("127.0.0.1", 0), server)

    def _insert_entity(self, entity_id):
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO entities (id, created_at, updated_at, last_accessed_at, title, full_content)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (entity_id, now, now, now, entity_id, f"Content for {entity_id}"),
        )

    def _insert_relation(self, source_id, target_id):
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at)
               VALUES (?, ?, ?, 'related_to', ?, ?, ?)""",
            (str(uuid4()), source_id, target_id, now, now, now),
        )

    @staticmethod
    def _capture(handler):
        captured = {}
        handler.send_json = lambda data, status=200: captured.update(data=data, status=status)
        return captured

    def test_gateway_is_read_only_and_bound_to_supplied_path(self):
        gateway = ViewerReadGateway(self.db_path)
        conn = gateway.connect()
        self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
        with self.assertRaises(Exception):
            conn.execute("CREATE TABLE forbidden_write (id INTEGER)")
        conn.close()

    def test_entities_and_events_validate_and_echo_limit(self):
        handler = self._handler()
        bad = self._capture(handler)
        handler.get_entities({"limit": ["0"]})
        self.assertEqual(bad["status"], 400)

        events = self._capture(handler)
        handler.get_events({"limit": ["1"]})
        self.assertEqual(events["status"], 200)
        self.assertEqual(events["data"]["limit"], 1)

    def test_static_manifest_and_retired_locks_are_not_public_data_surfaces(self):
        self.assertNotIn("/static/vendor/THIRD_PARTY.md", STATIC_ASSETS)
        handler = self._handler()
        captured = self._capture(handler)
        handler.path = "/api/locks"
        handler.do_GET()
        self.assertEqual(captured["status"], 410)
        self.assertEqual(captured["data"]["replacement"], "/api/operations")

    def test_shell_uses_local_sanitizer_assets_without_inline_handlers(self):
        shell = get_frontend_html()
        self.assertIn("/static/vendor/dompurify-3.4.10.min.js", shell)
        self.assertIn("/static/vendor/marked-18.0.7.umd.js", shell)
        self.assertNotIn("onclick=", shell)
        self.assertIn('id="event-detail"', shell)

    def test_neighborhood_validates_time_and_reports_real_truncation(self):
        self._insert_entity("root")
        self._insert_entity("child")
        self._insert_entity("second-child")
        self._insert_relation("root", "child")
        self._insert_relation("root", "second-child")
        self.conn.commit()
        handler = self._handler()
        invalid = self._capture(handler)
        handler.get_relations_neighborhood({"entity_id": ["root"], "as_of": ["not-a-time"]})
        self.assertEqual(invalid["status"], 400)

        exact = self._capture(handler)
        handler.get_relations_neighborhood({"entity_id": ["root"], "max_edges": ["1"]})
        self.assertEqual(exact["status"], 200)
        edge = exact["data"]["edges"][0]
        self.assertEqual(edge["source"], "root")
        self.assertIn(edge["target"], {"child", "second-child"})
        self.assertEqual(edge["predicate"], "related_to")
        self.assertEqual(exact["data"]["returned_edges"], 1)
        self.assertEqual(exact["data"]["total_matching_edges"], 2)
        self.assertEqual(exact["data"]["omitted_edge_count"], 1)
        self.assertTrue(exact["data"]["truncated"])

    def test_quality_and_operations_contracts(self):
        store_memory(
            content="Viewer rework contract test memory with sufficient useful content.",
            title="Viewer Contract Memory",
            owner_id="viewer_test",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        state = _DaemonState(self.db_path, "test", True)
        state.service_port = 12345
        state.viewer_port = 9876
        handler = self._handler(state)
        quality = self._capture(handler)
        handler.get_quality({})
        self.assertEqual(quality["status"], 200)
        self.assertIn("orphan_raw", quality["data"])

        operations = self._capture(handler)
        handler.get_operations()
        self.assertEqual(operations["status"], 200)
        self.assertEqual(operations["data"]["api_version"], 1)
        self.assertIn("active_hello_sessions", operations["data"]["daemon"])

    def test_explorer_filters_status_and_memory_type(self):
        now = datetime.now(UTC).isoformat()
        self.conn.executemany(
            """INSERT INTO entities (id, created_at, updated_at, last_accessed_at, title,
               full_content, status, memory_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("raw-fact", now, now, now, "Raw fact", "raw fact content", "raw", "fact"),
                (
                    "consolidated-decision",
                    now,
                    now,
                    now,
                    "Consolidated decision",
                    "decision content",
                    "consolidated",
                    "decision",
                ),
                (
                    "archived-fact",
                    now,
                    now,
                    now,
                    "Archived fact",
                    "archived content",
                    "archived",
                    "fact",
                ),
                ("raw-fact-two", now, now, now, "Raw fact two", "raw fact content", "raw", "fact"),
                (
                    "raw-fact-three",
                    now,
                    now,
                    now,
                    "Raw fact three",
                    "raw fact content",
                    "raw",
                    "fact",
                ),
            ],
        )
        self.conn.commit()
        handler = self._handler()
        archived = self._capture(handler)
        handler.get_entities({"status": ["archived"]})
        self.assertEqual([item["id"] for item in archived["data"]["entities"]], ["archived-fact"])

        decision = self._capture(handler)
        handler.get_entities({"memory_type": ["decision"]})
        self.assertEqual(
            [item["id"] for item in decision["data"]["entities"]], ["consolidated-decision"]
        )

        paged = self._capture(handler)
        handler.get_entities(
            {"status": ["raw"], "memory_type": ["fact"], "page": ["2"], "limit": ["2"]}
        )
        self.assertEqual(paged["data"]["page"], 2)
        self.assertEqual(paged["data"]["total_count"], 3)
        self.assertEqual(paged["data"]["total_pages"], 2)
        self.assertEqual(len(paged["data"]["entities"]), 1)
        self.assertEqual(paged["data"]["entities"][0]["status"], "raw")
        self.assertEqual(paged["data"]["entities"][0]["memory_type"], "fact")

    def test_frontend_remediation_contracts(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "src/saltmdb/viewer/static/viewer.js").read_text(encoding="utf-8")
        stylesheet = (root / "src/saltmdb/viewer/static/viewer.css").read_text(encoding="utf-8")
        shell = get_frontend_html()
        self.assertIn('id="connection-indicator"', shell)
        for expected in (
            "All statuses", "All types", "Page ${data.page} of ${data.total_pages", "renderGraph",
            "normalizeGraph", "edge.source", "edge.target", "malformed relations could not be rendered",
            "No active relations for this memory", "source?.title || edge.source",
            "button('Apply filters', 'primary', undefined, 'submit')",
            "const resetFilters = button('Reset filters'",
            "state.explorerPreset = {}; state.explorerPage = 1; render();",
            "button('Explore graph', 'primary', undefined, 'submit')", "type = 'button'",
            "focusRelationshipInput", "modalInvoker", "aria-busy", "Copy ID", "Browse / audit list",
            "Hybrid retrieval query", "/api/search?q=", "Keyword match in title and memory text",
            "date_field", "date_from", "date_to", "View details", "Browse this context",
            "memoryCell", "metadata-panel", "['Pending', embeddingData.pending, 'pending']",
            "['Failed', embeddingData.failed, 'failed']",
        ):
            self.assertIn(expected, script)
        self.assertNotIn("source_id", script)
        self.assertNotIn("target_id", script)
        self.assertIn(
            "Showing ${data.returned_edges} of ${data.total_matching_edges} relations; ${data.omitted_edge_count} omitted by limit.",
            script,
        )
        self.assertNotIn("New data may be available", script)
        for expected in (
            "--lifecycle-raw", "--lifecycle-consolidated", "--lifecycle-archived", "--state-ready",
            "--state-pending", "--state-failed", "--state-warning",
            ".metric-card.pending { border-top-color: var(--state-pending); }",
            ".metric-card.failed { border-top-color: var(--state-failed); }",
            ".row-button { display: block; width: 100%", "max-height: 95dvh", "text-align: left",
            ".predicate-pill",
        ):
            self.assertIn(expected, stylesheet)

    def test_frontend_core_filter_and_fact_pair_contracts(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "src/saltmdb/viewer/static/viewer.js").read_text(encoding="utf-8")
        stylesheet = (root / "src/saltmdb/viewer/static/viewer.css").read_text(encoding="utf-8")

        self.assertRegex(
            script,
            r"const core = checkboxField\('Core memories', state\.explorerPreset\.is_core === 'true'\);",
        )
        self.assertIn("element.type = 'checkbox'", script)
        self.assertIn("element.setAttribute('aria-label', label)", script)
        self.assertIn("core.wrap", script)
        self.assertIn("if (core.element.checked) params.set('is_core', 'true');", script)
        self.assertNotIn("params.set('is_core', 'false')", script)
        self.assertIn("state.explorerPreset = Object.fromEntries(params); state.explorerPage = 1;", script)
        self.assertIn("currentParams = new URLSearchParams(params)", script)
        self.assertIn("Core-memory filtering is available.", script)

        helper = re.search(
            r"const factPair = \(label, value\) => \{(?P<body>.*?)\n  \};",
            script,
            re.DOTALL,
        )
        self.assertIsNotNone(helper)
        self.assertIn("const pair = node('div')", helper.group("body"))
        self.assertIn("pair.append(node('dt', label), node('dd', value));", helper.group("body"))
        self.assertEqual(helper.group("body").count("node('dt'"), 1)
        self.assertEqual(helper.group("body").count("node('dd'"), 1)
        self.assertIn("metadataEntries.forEach(([label, value]) => metadataGrid.append(factPair(label, value)));", script)
        self.assertIn("customFacts.append(factPair(key, typeof value === 'string' ? value : JSON.stringify(value)))", script)
        self.assertIn("facts.append(factPair(label, value))", script)
        self.assertIn(".checkbox-field input { width: 1rem; min-width: 1rem;", stylesheet)
