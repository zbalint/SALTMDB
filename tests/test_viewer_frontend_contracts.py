"""Static-source contracts for viewer.js.

No browser, node or playwright is available (RAM-limited dev box), so these pin the wiring the
audit found missing. They cannot prove focus, keyboard or layout behavior at runtime.
"""

import unittest
from pathlib import Path

from saltmdb.viewer.templates import get_frontend_html


def _read_static(name):
    root = Path(__file__).resolve().parents[1] / "src/saltmdb/viewer/static"
    return (root / name).read_text(encoding="utf-8")


class TestViewerFrontendContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = _read_static("viewer.js")

    def _function_body(self, name, following):
        start = self.script.index(f"const {name} = ")
        return self.script[start : self.script.index(f"const {following} = ", start)]

    def test_system_health_renders_the_data_the_endpoint_provides(self):
        body = self._function_body("operations", "tags")
        for expected in (
            "data.warnings",
            "latest_snapshot",
            "vector.available",
            "wal_bytes",
            "shm_bytes",
            "page_count",
            "freelist_count",
            "daemon.version",
            "uptime_s",
            "No warnings",
        ):
            with self.subTest(expected):
                self.assertIn(expected, body)

    def test_scatterplot_points_are_keyboard_operable_and_report_sampling(self):
        body = self._function_body("diagnostics", "loaders")
        self.assertIn("addEventListener('keydown'", body)
        self.assertIn("event.key === 'Enter' || event.key === ' '", body)
        self.assertIn("event.preventDefault()", body)
        self.assertIn("scatterData.truncated", body)
        self.assertIn("scatterData.total_ready", body)

    def test_async_ui_actions_cannot_fail_silently(self):
        # Submit handlers and pagers run outside render()'s try/catch, so each must go through
        # the guarded() wrapper that reports failures in the notice bar.
        self.assertNotIn("addEventListener('submit', async", self.script)
        self.assertNotRegex(self.script, r"addEventListener\('submit', (?!guarded)")
        for pager_call in ("() => list(currentParams,", "() => list(page"):
            with self.subTest(pager_call):
                self.assertNotIn(f"button('Previous', '', {pager_call}", self.script)
                self.assertNotIn(f"button('Next', '', {pager_call}", self.script)
        self.assertGreaterEqual(self.script.count("guarded("), 12)

    def test_background_polling_pauses_while_reading_or_in_a_dialog(self):
        self.assertIn("const pollingPaused = () =>", self.script)
        pause = self.script[self.script.index("const pollingPaused") :]
        pause = pause[: pause.index("const schedule")]
        for dialog_name in ("dialog", "eventDialog", "traceDialog"):
            self.assertIn(dialog_name, pause)
        self.assertIn(".open", pause)
        self.assertIn("view.contains(document.activeElement)", pause)
        schedule = self.script[self.script.index("const schedule") :]
        self.assertIn("!pollingPaused()", schedule[: schedule.index("document.querySelectorAll")])
        self.assertIn("!pollingPaused()", self.script[self.script.index("visibilitychange") :])

    def test_navigation_and_raw_toggle_expose_state_to_assistive_tech(self):
        self.assertIn("setAttribute('aria-current', 'page')", self.script)
        self.assertIn("removeAttribute('aria-current')", self.script)
        self.assertIn("setAttribute('aria-pressed'", self.script)
        self.assertIn("'Show rendered'", self.script)
        self.assertIn("'Show raw'", self.script)

    def test_content_region_is_not_a_live_region_but_a_status_node_is(self):
        shell = get_frontend_html()
        self.assertNotIn('<section id="view" aria-live', shell)
        self.assertIn('id="live-status" role="status"', shell)

    def test_activity_and_session_timestamps_use_the_shared_formatter(self):
        self.assertIn("const timeCell = (value) =>", self.script)
        self.assertIn("cell.title = String(value)", self.script)
        self.assertIn(
            "timeCell(event.timestamp)", self._function_body("activity", "openSessionDetail")
        )
        self.assertIn(
            "timeCell(event.timestamp)", self.script[self.script.index("const sessionDetail") :]
        )
        self.assertIn(
            "formatTimestamp(event.timestamp)", self._function_body("openEventDetail", "activity")
        )


    def test_activity_view_has_filters_and_paging(self):
        body = self._function_body("activity", "openSessionDetail")
        for expected in (
            "inputField('Event type'",
            "inputField('Agent'",
            "inputField('Session ID'",
            "inputField('Context ID'",
            "inputField('Text'",
            "/api/events?${",
            "setAttribute('aria-label', 'Activity pages')",
            "state.activityPreset",
            "state.activityPage",
            "guarded(",
        ):
            with self.subTest(expected):
                self.assertIn(expected, body)
        self.assertNotIn("/api/events?limit=20", body)
        self.assertIn("activityPage: 1, activityPreset: {}", self.script)


if __name__ == "__main__":
    unittest.main()
