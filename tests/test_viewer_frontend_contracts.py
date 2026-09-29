"""Static-source contracts for viewer.js.

No browser, node or playwright is available (RAM-limited dev box), so these pin the wiring the
audit found missing. They cannot prove focus, keyboard or layout behavior at runtime.
"""

import re
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

    def test_activity_view_is_fully_renamed_to_events(self):
        for stale in ("activityPreset", "activityPage", "activity:", "'activity'", "Activity"):
            with self.subTest(stale):
                self.assertNotIn(stale, self.script)
        self.assertIn("events: 'Events'", self.script)

    def test_live_feed_is_wired_with_its_own_timer_not_the_full_rerender_poll(self):
        self.assertIn("feed: 'Live feed'", self.script)
        self.assertRegex(self.script, r"const loaders = \{[^}]*\bfeed\b[^}]*\}")
        poll_lists = re.findall(r"\[('overview'[^\]]*)\]\.includes\(state\.view\)", self.script)
        self.assertTrue(poll_lists)
        for poll_list in poll_lists:
            self.assertNotIn("'feed'", poll_list)
        # Leaving the feed (any render) must stop its timer; unloading the page too.
        render = self.script[self.script.index("const render = ") :]
        self.assertIn("clearInterval(state.feedTimer)", render[: render.index("try {")])
        self.assertIn(
            "clearInterval(state.feedTimer)", self.script[self.script.index("'beforeunload'") :]
        )

    def test_session_colors_are_a_stable_class_based_palette_apart_from_semantic_colors(self):
        css = _read_static("viewer.css")
        palette = dict(re.findall(r"--session-(\d+):\s*(#[0-9a-fA-F]{6})", css))
        self.assertGreaterEqual(len(palette), 6)
        semantic = {
            value.lower()
            for name, value in re.findall(
                r"--(lifecycle-[a-z]+|state-[a-z]+):\s*(#[0-9a-fA-F]{6})", css
            )
        }
        self.assertTrue(semantic)
        self.assertFalse(semantic & {value.lower() for value in palette.values()})
        for index in palette:
            with self.subTest(index):
                self.assertRegex(
                    css,
                    rf"\.session-color-{index}\s*\{{[^}}]*--session-color:\s*var\(--session-{index}\)",
                )
        body = self._function_body("sessionColorClass", "feed")
        # Pure function of the id (no counters/maps that depend on arrival order), class based
        # because the CSP forbids inline style attributes.
        self.assertIn("% SESSION_COLORS", body)
        self.assertIn("`session-color-${", body)
        self.assertIn(f"const SESSION_COLORS = {len(palette)};", self.script)
        self.assertNotIn(".style", body)
        self.assertNotIn("setAttribute('style'", self.script)

    def test_feed_rows_show_kind_and_session_chip_that_filters_the_feed(self):
        body = self._function_body("feedRow", "feed")
        for expected in (
            "sessionColorClass(item.session_id)",
            "feed-kind",
            "feed-session",
            "state.feedSession = item.session_id",
            "formatTimestamp(item.timestamp)",
            "openDetail(item.id",
            "openTraceDetail(item.id",
        ):
            with self.subTest(expected):
                self.assertIn(expected, body)
        # Session ids and previews are untrusted display text, never markup.
        self.assertNotIn("innerHTML", body)
        # The chip carries readable text, so colour is never the only session signal.
        self.assertIn("item.session_id.slice(0, 6)", body)

    def test_feed_polls_incrementally_from_a_cursor_without_rerendering_the_view(self):
        body = self._function_body("feed", "presetKeys")
        for expected in (
            "/api/feed?",
            "since: cursor",
            "active_only",
            "state.feedSession",
            "data.liveness_known",
            "data.has_more",
            "document.hidden",
            "state.feedTimer = setInterval(",
            "FEED_MAX_ROWS",
        ):
            with self.subTest(expected):
                self.assertIn(expected, body)
        poll = body[body.index("const pollFeed = ") :]
        self.assertNotIn("view.replaceChildren", poll)
        self.assertNotIn("render()", poll)
        # A trace that completes resurfaces as the same item: replace it in place by key.
        self.assertIn("rows.get(key)?.remove()", body)

    def test_feed_rows_are_styled_with_the_session_colour(self):
        css = _read_static("viewer.css")
        self.assertRegex(css, r"\.feed-row\s*\{[^}]*border-left:[^;}]*var\(--session-color")
        self.assertRegex(css, r"\.feed-session\s*\{[^}]*color:\s*var\(--session-color")
        # A default declared on .feed-row would sit after the .session-color-N rules at equal
        # specificity and override every session colour; only a var() fallback is safe.
        self.assertNotRegex(css, r"\.feed-row\s*\{[^}]*--session-color:")
        for selector in (".feed-list", ".feed-kind", ".feed-head", ".feed-filter"):
            with self.subTest(selector):
                self.assertIn(selector, css)

    def test_overview_shows_trace_counts_from_stats(self):
        body = self._function_body("overview", "loaders")
        for expected in ("data.traces_last_24h", "data.total_traces"):
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

    def test_events_and_session_timestamps_use_the_shared_formatter(self):
        self.assertIn("const timeCell = (value) =>", self.script)
        self.assertIn("cell.title = String(value)", self.script)
        self.assertIn(
            "timeCell(event.timestamp)", self._function_body("events", "openSessionDetail")
        )
        self.assertIn(
            "timeCell(event.timestamp)", self.script[self.script.index("const sessionDetail") :]
        )
        self.assertIn(
            "formatTimestamp(event.timestamp)", self._function_body("openEventDetail", "events")
        )

    def test_events_view_has_filters_and_paging(self):
        body = self._function_body("events", "openSessionDetail")
        for expected in (
            "inputField('Event type'",
            "inputField('Agent'",
            "inputField('Session ID'",
            "inputField('Context ID'",
            "inputField('Text'",
            "/api/events?${",
            "setAttribute('aria-label', 'Event pages')",
            "state.eventsPreset",
            "state.eventsPage",
            "guarded(",
        ):
            with self.subTest(expected):
                self.assertIn(expected, body)
        self.assertNotIn("/api/events?limit=20", body)
        self.assertIn("eventsPage: 1, eventsPreset: {}", self.script)

    def test_memory_map_exposes_the_bounded_neighborhood_controls(self):
        body = self._function_body("relationships", "quality")
        for expected in (
            "select('Depth'",
            "inputField('Predicate'",
            "checkboxField('Include archived'",
            "inputField('As of (UTC)'",
            "depth",
            "predicate",
            "exclude_archived",
            "as_of",
            "state.relationOptions",
            "graph-legend",
        ):
            with self.subTest(expected):
                self.assertIn(expected, body)
        self.assertIn("relationOptions: {}", self.script)

    def test_view_state_lives_in_the_url_for_refresh_and_back_forward(self):
        for expected in (
            "const encodeViewState = ()",
            "const applyViewState = (hash)",
            "history.pushState",
            "history.replaceState",
            "addEventListener('popstate'",
            "applyViewState(location.hash)",
        ):
            with self.subTest(expected):
                self.assertIn(expected, self.script)
        # The saved state must be applied before the very first render.
        boot = self.script[self.script.index("connection('checking'") :]
        self.assertLess(boot.index("applyViewState(location.hash)"), boot.index("render()"))
        # Every filterable view's preset and page participates.
        encode = self.script[
            self.script.index("const presetKeys") : self.script.index("const applyViewState")
        ]
        for token in (
            "explorerPreset",
            "eventsPreset",
            "sessionsPreset",
            "qualityPreset",
            "sessionDetailId",
            "relationRoot",
        ):
            with self.subTest(token):
                self.assertIn(token, encode)


if __name__ == "__main__":
    unittest.main()
