"""Tests for core_governance_service.py -- the sole authority for core-memory bootstrap
governance (see plans/core_memory_bootstrap_governance_detailed.md)."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone

from saltmdb.config import (
    CORE_MAX_ACTIVE,
    CORE_MAX_CONTENT_CHARS,
    CORE_REASON_MAX_CHARS,
    CORE_REASON_MIN_CHARS,
    CORE_EXIT_MAX_CHARS,
    CORE_EXIT_MIN_CHARS,
    CORE_REVIEW_RATIONALE_MAX_CHARS,
    CORE_REVIEW_RATIONALE_MIN_CHARS,
    CORE_MAX_REVIEW_DAYS,
    CORE_DEFAULT_REVIEW_DAYS,
)
from saltmdb.db.schema import init_db
from saltmdb.domain.services import core_governance_service as cgs
from saltmdb.domain.services.memory_service import store_memory


REASON = "A" * CORE_REASON_MIN_CHARS
EXIT = "B" * CORE_EXIT_MIN_CHARS


class TestParseIsCore(unittest.TestCase):
    def test_none_stays_none(self):
        self.assertIsNone(cgs.parse_is_core(None))

    def test_true_false_pass_through(self):
        self.assertIs(cgs.parse_is_core(True), True)
        self.assertIs(cgs.parse_is_core(False), False)

    def test_ambiguous_string_rejected(self):
        with self.assertRaises(ValueError):
            cgs.parse_is_core("yes")

    def test_integer_rejected(self):
        with self.assertRaises(ValueError):
            cgs.parse_is_core(1)
        with self.assertRaises(ValueError):
            cgs.parse_is_core(0)

    def test_arbitrary_object_rejected(self):
        with self.assertRaises(ValueError):
            cgs.parse_is_core({"a": 1})


class TestBoundedTextValidation(unittest.TestCase):
    def test_core_reason_exact_boundaries(self):
        cgs.validate_core_reason("x" * CORE_REASON_MIN_CHARS)
        cgs.validate_core_reason("x" * CORE_REASON_MAX_CHARS)
        with self.assertRaises(ValueError):
            cgs.validate_core_reason("x" * (CORE_REASON_MIN_CHARS - 1))
        with self.assertRaises(ValueError):
            cgs.validate_core_reason("x" * (CORE_REASON_MAX_CHARS + 1))

    def test_core_exit_condition_exact_boundaries(self):
        cgs.validate_core_exit_condition("x" * CORE_EXIT_MIN_CHARS)
        cgs.validate_core_exit_condition("x" * CORE_EXIT_MAX_CHARS)
        with self.assertRaises(ValueError):
            cgs.validate_core_exit_condition("x" * (CORE_EXIT_MIN_CHARS - 1))
        with self.assertRaises(ValueError):
            cgs.validate_core_exit_condition("x" * (CORE_EXIT_MAX_CHARS + 1))

    def test_review_rationale_exact_boundaries(self):
        cgs.validate_core_review_rationale("x" * CORE_REVIEW_RATIONALE_MIN_CHARS)
        cgs.validate_core_review_rationale("x" * CORE_REVIEW_RATIONALE_MAX_CHARS)
        with self.assertRaises(ValueError):
            cgs.validate_core_review_rationale("x" * (CORE_REVIEW_RATIONALE_MIN_CHARS - 1))
        with self.assertRaises(ValueError):
            cgs.validate_core_review_rationale("x" * (CORE_REVIEW_RATIONALE_MAX_CHARS + 1))

    def test_none_or_empty_rejected(self):
        with self.assertRaises(ValueError):
            cgs.validate_core_reason(None)
        with self.assertRaises(ValueError):
            cgs.validate_core_reason("   ")

    def test_crlf_normalized_before_length_check(self):
        # A CRLF-heavy string collapses to fewer chars after LF normalization -- validated post-
        # normalization, matching full_content's own existing normalization order.
        raw = ("x\r\n" * (CORE_REASON_MIN_CHARS // 2 + 5)).strip()
        normalized = cgs.validate_core_reason(raw)
        self.assertNotIn("\r", normalized)

    def test_secret_redacted_before_length_and_persistence(self):
        secret_bearing = f"{'x' * CORE_REASON_MIN_CHARS} api_key=abcdefgh12345678"
        normalized = cgs.validate_core_reason(secret_bearing)
        self.assertNotIn("abcdefgh12345678", normalized)
        self.assertIn("[REDACTED_SECRET]", normalized)


class TestCoreContentLength(unittest.TestCase):
    def test_at_limit_passes(self):
        cgs.validate_core_content_length("x" * CORE_MAX_CONTENT_CHARS)

    def test_over_limit_rejected(self):
        with self.assertRaises(ValueError):
            cgs.validate_core_content_length("x" * (CORE_MAX_CONTENT_CHARS + 1))


class TestReviewAfter(unittest.TestCase):
    def test_default_days_applied_when_omitted(self):
        now = datetime.now(UTC)
        result = cgs.parse_core_review_after(None, default_days=CORE_DEFAULT_REVIEW_DAYS, now=now)
        expected = now + timedelta(days=CORE_DEFAULT_REVIEW_DAYS)
        self.assertEqual(datetime.fromisoformat(result), expected)

    def test_missing_value_and_default_raises(self):
        with self.assertRaises(ValueError):
            cgs.parse_core_review_after(None, default_days=None)

    def test_max_30_days_boundary(self):
        now = datetime.now(UTC)
        at_max = (now + timedelta(days=CORE_MAX_REVIEW_DAYS)).isoformat()
        cgs.parse_core_review_after(at_max, now=now)  # must not raise
        over_max = (now + timedelta(days=CORE_MAX_REVIEW_DAYS, seconds=1)).isoformat()
        with self.assertRaises(ValueError):
            cgs.parse_core_review_after(over_max, now=now)

    def test_malformed_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            cgs.parse_core_review_after("not-a-timestamp")

    def test_is_overdue_equality_at_boundary_counts_as_due(self):
        now = datetime.now(UTC)
        self.assertTrue(cgs.is_overdue(now.isoformat(), now=now))
        self.assertFalse(cgs.is_overdue((now + timedelta(seconds=1)).isoformat(), now=now))
        self.assertTrue(cgs.is_overdue((now - timedelta(seconds=1)).isoformat(), now=now))

    def test_is_overdue_missing_or_malformed_counts_as_due(self):
        self.assertTrue(cgs.is_overdue(None))
        self.assertTrue(cgs.is_overdue(""))
        self.assertTrue(cgs.is_overdue("garbage"))

    # Resolved review finding #7: timestamps must persist in one canonical, timezone-aware UTC
    # representation, and every create/promote/consolidate/retain path must reject a
    # past-or-equal review timestamp, not just retain.

    def test_non_utc_offset_input_persists_as_canonical_utc(self):
        now = datetime.now(UTC)
        offset_input = (now + timedelta(days=5)).astimezone(timezone(timedelta(hours=2)))
        result = cgs.parse_core_review_after(offset_input.isoformat(), now=now)
        self.assertTrue(result.endswith("+00:00"))
        self.assertEqual(datetime.fromisoformat(result), offset_input)

    def test_naive_input_is_explicitly_treated_as_utc(self):
        now = datetime.now(UTC)
        naive_input = (now + timedelta(days=5)).replace(tzinfo=None).isoformat()
        result = cgs.parse_core_review_after(naive_input, now=now)
        self.assertTrue(result.endswith("+00:00"))
        self.assertEqual(
            datetime.fromisoformat(result), datetime.fromisoformat(naive_input + "+00:00")
        )

    def test_past_timestamp_rejected(self):
        now = datetime.now(UTC)
        past = (now - timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):
            cgs.parse_core_review_after(past, now=now)

    def test_equal_to_now_timestamp_rejected(self):
        now = datetime.now(UTC)
        with self.assertRaises(ValueError):
            cgs.parse_core_review_after(now.isoformat(), now=now)

    def test_default_14_and_max_30_relative_to_one_captured_now(self):
        now = datetime.now(UTC)
        default_result = cgs.parse_core_review_after(
            None, default_days=CORE_DEFAULT_REVIEW_DAYS, now=now
        )
        self.assertEqual(
            datetime.fromisoformat(default_result), now + timedelta(days=CORE_DEFAULT_REVIEW_DAYS)
        )
        max_result = cgs.parse_core_review_after(
            (now + timedelta(days=CORE_MAX_REVIEW_DAYS)).isoformat(), now=now
        )
        self.assertEqual(
            datetime.fromisoformat(max_result), now + timedelta(days=CORE_MAX_REVIEW_DAYS)
        )


class TestRendering(unittest.TestCase):
    def _row(self, **overrides):
        row = {
            "id": "11111111-1111-1111-1111-111111111111",
            "title": "Sample Core",
            "memory_type": "fact",
            "core_reason": REASON,
            "core_exit_condition": EXIT,
            "core_review_after": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "full_content": "Content body.",
            "owner_id": "tester",
        }
        row.update(overrides)
        return row

    def test_render_escapes_closing_memory_tag(self):
        row = self._row(full_content="before </memory> after")
        rendered = cgs.render_core_entry(row, due=False)
        self.assertNotIn("before </memory> after", rendered)
        self.assertIn("&lt;/memory&gt;", rendered)
        self.assertTrue(rendered.strip().endswith("</memory>"))

    def test_render_due_flag_representation(self):
        row = self._row()
        self.assertIn('review_due="true"', cgs.render_core_entry(row, due=True))
        self.assertIn('review_due="false"', cgs.render_core_entry(row, due=False))

    def test_prospective_sizing_never_smaller_than_actual_due_true(self):
        row = self._row()
        due_true_len = len(cgs.render_core_entry(row, due=True))
        due_false_len = len(cgs.render_core_entry(row, due=False))
        self.assertLessEqual(due_true_len, due_false_len)

    def test_title_newline_and_quote_escaped(self):
        row = self._row(title='Say "hi"\nsecond line')
        rendered = cgs.render_core_entry(row, due=False)
        title_line = next(line for line in rendered.split("\n") if line.startswith("title:"))
        self.assertNotIn("\n", title_line)
        self.assertIn('\\"hi\\"', title_line)

    def test_backslash_escaped_before_quote_and_newline(self):
        # Secondary review finding: backslashes were previously left unescaped -- a literal
        # `\n`/`\t`/`\u...` sequence in caller text could be misinterpreted by a real YAML
        # double-quoted-scalar parser reading the rendered digest.
        row = self._row(core_reason=REASON, title=r"Contains \n literally, not a newline")
        rendered = cgs.render_core_entry(row, due=False)
        title_line = next(line for line in rendered.split("\n") if line.startswith("title:"))
        # The raw two-character sequence backslash-n must be escaped to backslash-backslash-n --
        # a real YAML parser must see it as a literal backslash followed by 'n', never as an
        # escaped-newline control sequence.
        self.assertIn(r"\\n", title_line)

    def test_build_inventory_never_includes_full_content(self):
        row = self._row(full_content="SECRET CONTENT MUST NOT LEAK")
        inventory = cgs.build_inventory([row])
        self.assertEqual(len(inventory), 1)
        item = inventory[0]
        self.assertNotIn("full_content", item)
        self.assertNotIn("SECRET CONTENT MUST NOT LEAK", json.dumps(item))


class TestBoundedBootstrapErrorReport(unittest.TestCase):
    """Resolved review finding #6: render_bootstrap_error previously appended one inventory line
    per row with no character budget -- a synthetic 100-row corrupt set with max-length titles
    produced 26,901 characters, well past the 15,000-char envelope this feature protects."""

    def _corrupt_row(self, i):
        return {
            "id": f"{i:08d}-0000-0000-0000-000000000000",
            "title": f"Corrupt Core Title Number {i} " + ("T" * 120),
            "memory_type": "fact",
            "core_reason": None,
            "core_exit_condition": None,
            "core_review_after": None,
            "full_content": "x" * 50,
            "owner_id": f"owner_{i}",
        }

    def test_hundreds_of_corrupt_rows_stay_under_cap_and_well_formed(self):
        from saltmdb.config import CORE_BOOTSTRAP_ERROR_MAX_CHARS

        rows = [self._corrupt_row(i) for i in range(300)]
        violations = [f"{r['id']}: core_reason missing or out of bounds" for r in rows] + [
            f"{r['id']}: core_exit_condition missing or out of bounds" for r in rows
        ]
        report = cgs.render_bootstrap_error(rows, violations)
        self.assertLessEqual(len(report), CORE_BOOTSTRAP_ERROR_MAX_CHARS)
        self.assertTrue(report.strip().endswith("</saltmdb-digest>"))
        self.assertIn("</core-bootstrap-error>", report)
        self.assertIn("omitted_core_count=", report)
        self.assertIn("omitted_violation_count=", report)
        # At least one of the two sections was actually truncated at this scale -- otherwise the
        # test would not be exercising the bound at all.
        self.assertFalse(
            report.count("omitted_core_count=0") == 1 and "omitted_violation_count=0" in report
        )

    def test_thousands_of_corrupt_rows_never_exceed_cap(self):
        from saltmdb.config import CORE_BOOTSTRAP_ERROR_MAX_CHARS

        rows = [self._corrupt_row(i) for i in range(3000)]
        violations = [f"{r['id']}: core_reason missing or out of bounds" for r in rows]
        report = cgs.render_bootstrap_error(rows, violations)
        self.assertLessEqual(len(report), CORE_BOOTSTRAP_ERROR_MAX_CHARS)
        self.assertTrue(report.strip().endswith("</saltmdb-digest>"))

    def test_long_titles_and_owners_cannot_break_the_cap_or_structure(self):
        from saltmdb.config import CORE_BOOTSTRAP_ERROR_MAX_CHARS

        rows = [self._corrupt_row(i) for i in range(50)]
        for r in rows:
            r["title"] = "Q" * 5000
            r["owner_id"] = "R" * 5000
        violations = [f"{r['id']}: core_reason missing or out of bounds" for r in rows]
        report = cgs.render_bootstrap_error(rows, violations)
        self.assertLessEqual(len(report), CORE_BOOTSTRAP_ERROR_MAX_CHARS)
        self.assertTrue(report.strip().endswith("</saltmdb-digest>"))

    def test_no_full_content_appears_in_the_error_report(self):
        rows = [self._corrupt_row(i) for i in range(5)]
        for r in rows:
            r["full_content"] = f"SECRET_BODY_{r['id']}_MUST_NOT_LEAK"
        violations = [f"{r['id']}: core_reason missing or out of bounds" for r in rows]
        report = cgs.render_bootstrap_error(rows, violations)
        for r in rows:
            self.assertNotIn(f"SECRET_BODY_{r['id']}_MUST_NOT_LEAK", report)

    def test_small_corrupt_set_reports_zero_omissions(self):
        rows = [self._corrupt_row(0)]
        violations = ["some violation"]
        report = cgs.render_bootstrap_error(rows, violations)
        self.assertIn("omitted_core_count=0", report)
        self.assertIn("omitted_violation_count=0", report)


class TestFindInvariantViolations(unittest.TestCase):
    def _valid_row(self, **overrides):
        row = {
            "id": "22222222-2222-2222-2222-222222222222",
            "title": "Valid Core",
            "memory_type": "fact",
            "core_reason": REASON,
            "core_exit_condition": EXIT,
            "core_review_after": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "full_content": "Valid content.",
            "owner_id": "tester",
            "created_at": datetime.now(UTC).isoformat(),
            "scope": "shared",
        }
        row.update(overrides)
        return row

    def test_valid_set_has_no_violations(self):
        self.assertEqual(cgs.find_invariant_violations([self._valid_row()]), [])

    def test_too_many_active_cores_flagged(self):
        rows = [self._valid_row(id=f"id-{i}") for i in range(CORE_MAX_ACTIVE + 1)]
        violations = cgs.find_invariant_violations(rows)
        self.assertTrue(any("exceeds CORE_MAX_ACTIVE" in v for v in violations))

    def test_private_scope_flagged(self):
        violations = cgs.find_invariant_violations([self._valid_row(scope="private")])
        self.assertTrue(any("scope must be shared" in v for v in violations))

    def test_oversized_content_flagged(self):
        violations = cgs.find_invariant_violations(
            [self._valid_row(full_content="x" * (CORE_MAX_CONTENT_CHARS + 1))]
        )
        self.assertTrue(any("full_content exceeds" in v for v in violations))

    def test_missing_reason_flagged(self):
        violations = cgs.find_invariant_violations([self._valid_row(core_reason=None)])
        self.assertTrue(any("core_reason" in v for v in violations))

    def test_malformed_review_after_flagged(self):
        violations = cgs.find_invariant_violations(
            [self._valid_row(core_review_after="not-a-date")]
        )
        self.assertTrue(any("core_review_after malformed" in v for v in violations))

    def test_overdue_alone_is_not_a_violation(self):
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        violations = cgs.find_invariant_violations([self._valid_row(core_review_after=past)])
        self.assertEqual(violations, [])

    def test_oversized_rendered_digest_flagged(self):
        big_row = self._valid_row(full_content="x" * CORE_MAX_CONTENT_CHARS)
        # 6 max-content rows: exceeds the rendered-size limit independent of the count limit
        # (both violations may legitimately coexist -- this test only cares about the former).
        rows = [{**big_row, "id": f"id-{i}"} for i in range(6)]
        violations = cgs.find_invariant_violations(rows)
        self.assertTrue(any("exceeds CORE_MAX_RENDERED_CHARS" in v for v in violations))


class CoreGovernanceDbTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store_core(self, title, content=None, **kwargs):
        # Fixtures use distinct content so core-governance tests remain independent of duplicate
        # handling.
        res = store_memory(
            title=title,
            content=content or f"Distinct fixture content body for {title}, not a near-duplicate.",
            owner_id=kwargs.pop("owner_id", "tester"),
            is_core=True,
            core_reason=kwargs.pop("core_reason", REASON),
            core_exit_condition=kwargs.pop("core_exit_condition", EXIT),
            db_connection=self.conn,
            **kwargs,
        )
        return res

    def _store_normal(self, title, content=None, **kwargs):
        return store_memory(
            title=title,
            content=content or f"Distinct fixture content body for {title}, not a near-duplicate.",
            owner_id=kwargs.pop("owner_id", "tester"),
            db_connection=self.conn,
            **kwargs,
        )


class TestStoreMemoryCoreCreation(CoreGovernanceDbTestBase):
    def test_missing_core_reason_rejected(self):
        res = store_memory(
            title="Missing Reason Core",
            content="Content long enough to clear the quality gate's minimum length.",
            owner_id="tester",
            is_core=True,
            core_exit_condition=EXIT,
            db_connection=self.conn,
        )
        self.assertTrue(res.startswith("Error"), res)
        self.assertIn("core_reason", res)

    def test_missing_core_exit_condition_rejected(self):
        res = store_memory(
            title="Missing Exit Core",
            content="Content long enough to clear the quality gate's minimum length.",
            owner_id="tester",
            is_core=True,
            core_reason=REASON,
            db_connection=self.conn,
        )
        self.assertTrue(res.startswith("Error"), res)
        self.assertIn("core_exit_condition", res)

    def test_private_scope_core_rejected(self):
        res = self._store_core("Private Core Attempt", scope="private")
        self.assertTrue(res.startswith("Error"), res)
        self.assertIn("shared", res)

    def test_valid_core_succeeds_and_persists_lifecycle_fields(self):
        res = self._store_core("Valid Core Memory")
        self.assertEqual(res["status"], "ok")
        entity_id = res["data"]["id"]
        row = self.conn.execute(
            "SELECT is_core, core_reason, core_exit_condition, core_review_after FROM entities "
            "WHERE id = ?",
            (entity_id,),
        ).fetchone()
        self.assertTrue(row[0])
        self.assertEqual(row[1], REASON)
        self.assertEqual(row[2], EXIT)
        self.assertIsNotNone(row[3])

    def test_core_only_fields_rejected_on_non_core_write(self):
        res = store_memory(
            title="Not Actually Core",
            content="Content long enough to clear the quality gate's minimum length.",
            owner_id="tester",
            core_reason=REASON,
            db_connection=self.conn,
        )
        self.assertTrue(res.startswith("Error"), res)
        self.assertIn("not core", res)

    def test_content_over_2500_chars_rejected(self):
        res = self._store_core("Oversized Core", content="x" * (CORE_MAX_CONTENT_CHARS + 1))
        self.assertEqual(res["status"], "rejected")

    def test_omitting_lifecycle_fields_on_update_preserves_existing(self):
        res = self._store_core("Preserve On Update")
        entity_id = res["data"]["id"]

        res2 = store_memory(
            entity_id=entity_id,
            title="Preserve On Update",
            content="Distinct fixture content body for Preserve On Update, not a near-duplicate.",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertEqual(res2["status"], "ok")
        row = self.conn.execute(
            "SELECT core_reason, core_exit_condition FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertEqual(row[0], REASON)
        self.assertEqual(row[1], EXIT)

    def test_new_core_with_past_review_after_rejected(self):
        # Resolved review finding #7: previously only review_core_memory(outcome='retain')
        # enforced a future-only core_review_after -- create/promote/consolidate could persist a
        # core that was immediately overdue.
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        res = self._store_core("Born Overdue Core Attempt", core_review_after=past)
        self.assertTrue(res.startswith("Error"), res)


class TestCapacityAdmission(CoreGovernanceDbTestBase):
    def test_sixth_core_rejected(self):
        for i in range(CORE_MAX_ACTIVE):
            res = self._store_core(f"Core Number {i}", content=f"Distinct content body number {i}.")
            self.assertEqual(res["status"], "ok")

        rejected = self._store_core("Core Number Six", content="Distinct content body number six.")
        self.assertIsInstance(rejected, dict)
        self.assertEqual(rejected["status"], "REJECTED")
        self.assertEqual(rejected["error_code"], "CORE_CAPACITY_EXCEEDED")
        self.assertIn("count", rejected["violated_dimensions"])
        self.assertEqual(len(rejected["inventory"]), CORE_MAX_ACTIVE)
        # Secondary review finding: agents were previously missing the exact amount they must
        # reduce, and current_totals had no rendered-size figure to compare against.
        self.assertIn("rendered_chars", rejected["current_totals"])
        self.assertIn("rendered_chars", rejected["proposed_totals"])
        self.assertIn("count", rejected["required_reduction"])
        self.assertEqual(rejected["required_reduction"]["count"], 1)

    def test_rejection_has_zero_side_effects(self):
        for i in range(CORE_MAX_ACTIVE):
            self._store_core(f"Zero Effect Core {i}", content=f"Distinct content body {i}.")
        before_count = self.conn.execute(
            "SELECT COUNT(*) FROM entities WHERE is_core = 1 AND status != 'archived'"
        ).fetchone()[0]

        self._store_core("Rejected Core", content="Distinct content body rejected.")

        after_count = self.conn.execute(
            "SELECT COUNT(*) FROM entities WHERE is_core = 1 AND status != 'archived'"
        ).fetchone()[0]
        self.assertEqual(before_count, after_count)
        rejected_row = self.conn.execute(
            "SELECT 1 FROM entities WHERE title = 'Rejected Core'"
        ).fetchone()
        self.assertIsNone(rejected_row)

    def test_archived_core_does_not_count_toward_capacity(self):
        from saltmdb.domain.services.memory_service import archive_memory

        ids = []
        for i in range(CORE_MAX_ACTIVE):
            res = self._store_core(f"Archivable Core {i}", content=f"Distinct content body {i}.")
            ids.append(res["data"]["id"])
        archive_memory(entity_id=ids[0], db_connection=self.conn)

        # Now only CORE_MAX_ACTIVE - 1 active cores exist -- a new one should be admitted.
        res = self._store_core("Replacement Core", content="Distinct replacement content body.")
        self.assertEqual(res["status"], "ok")

    def test_explicit_entity_id_does_not_bypass_capacity(self):
        for i in range(CORE_MAX_ACTIVE):
            self._store_core(f"Bypass Attempt Core {i}", content=f"Distinct content body {i}.")

        rejected = store_memory(
            entity_id="99999999-9999-9999-9999-999999999999",
            title="Bypass Via Explicit ID",
            content="Distinct content body for the bypass attempt.",
            owner_id="tester",
            is_core=True,
            core_reason=REASON,
            core_exit_condition=EXIT,
            db_connection=self.conn,
        )
        self.assertIsInstance(rejected, dict)
        self.assertEqual(rejected["status"], "REJECTED")

    def test_duplicate_policy_does_not_bypass_capacity(self):
        for i in range(CORE_MAX_ACTIVE):
            self._store_core(f"Skip Dup Core {i}", content=f"Distinct content body {i}.")

        rejected = self._store_core(
            "Skip Dup Bypass Attempt",
            content="Distinct content body bypass.",
        )
        self.assertIsInstance(rejected, dict)
        self.assertEqual(rejected["status"], "REJECTED")


class TestDetailMemoryIds(CoreGovernanceDbTestBase):
    def test_nonexistent_detail_id_rejected(self):
        fake_uuid = "33333333-3333-3333-3333-333333333333"
        res = self._store_core(
            "Core With Bad Detail",
            content=f"Content mentioning {fake_uuid} but the detail doesn't exist.",
            detail_memory_ids=[fake_uuid],
        )
        self.assertTrue(res.startswith("Error"), res)

    def test_private_detail_rejected(self):
        detail_res = self._store_normal("Private Detail Candidate", scope="private")
        detail_id = detail_res["data"]["id"]
        res = self._store_core(
            "Core With Private Detail",
            content=f"References Private Detail Candidate ({detail_id}) as a detail.",
            detail_memory_ids=[detail_id],
        )
        self.assertTrue(res.startswith("Error"), res)

    def test_core_detail_rejected(self):
        detail_res = self._store_core("Core Detail Candidate")
        detail_id = detail_res["data"]["id"]
        res = self._store_core(
            "Core With Core Detail",
            content=f"References Core Detail Candidate ({detail_id}) as a detail.",
            detail_memory_ids=[detail_id],
        )
        self.assertTrue(res.startswith("Error"), res)

    def test_missing_title_and_uuid_mention_rejected(self):
        detail_res = self._store_normal("Real Detail Memory")
        detail_id = detail_res["data"]["id"]
        res = self._store_core(
            "Core Missing Mention",
            content="This content never mentions the detail by title or UUID.",
            detail_memory_ids=[detail_id],
        )
        self.assertTrue(res.startswith("Error"), res)

    def test_valid_detail_creates_elaborates_on_edge(self):
        detail_res = self._store_normal("Genuine Detail Memory")
        detail_id = detail_res["data"]["id"]
        core_res = self._store_core(
            "Core With Valid Detail",
            content=f"See Genuine Detail Memory ({detail_id}) for full rationale and evidence.",
            detail_memory_ids=[detail_id],
        )
        self.assertEqual(core_res["status"], "ok")
        core_id = core_res["data"]["id"]

        rel = self.conn.execute(
            "SELECT predicate FROM relations WHERE source_id = ? AND target_id = ? AND valid_to IS NULL",
            (detail_id, core_id),
        ).fetchone()
        self.assertIsNotNone(rel)
        self.assertEqual(rel[0], "elaborates_on")

    def test_more_than_three_details_rejected(self):
        detail_ids = []
        for i in range(4):
            r = self._store_normal(f"Detail Memory {i}")
            detail_ids.append(r["data"]["id"])
        content = "Mentions " + " ".join(
            f"Detail Memory {i} ({did})" for i, did in enumerate(detail_ids)
        )
        res = self._store_core(
            "Core With Too Many Details", content=content, detail_memory_ids=detail_ids
        )
        self.assertTrue(res.startswith("Error"), res)

    def test_omitting_detail_ids_while_retaining_references_succeeds(self):
        # Resolved review finding #4: detail_memory_ids=None means "preserve", but the effective
        # (preserved) declaration is now always revalidated against the NEW content -- this must
        # still succeed when the new content keeps every required title/UUID mention.
        detail_content = (
            "Full rationale and supporting evidence for the retained reference detail claim, "
            "kept as a separate linked memory so the core itself stays short."
        )
        detail_res = self._store_normal("Retained Reference Detail", content=detail_content)
        detail_id = detail_res["data"]["id"]
        core_res = self._store_core(
            "Core Retaining Detail Reference",
            content=f"See Retained Reference Detail ({detail_id}): {detail_content}",
            detail_memory_ids=[detail_id],
        )
        core_id = core_res["data"]["id"]

        updated = store_memory(
            entity_id=core_id,
            title="Core Retaining Detail Reference",
            content=f"See Retained Reference Detail ({detail_id}): {detail_content}",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertEqual(updated["status"], "ok")
        row = self.conn.execute(
            "SELECT core_detail_memory_ids FROM entities WHERE id = ?", (core_id,)
        ).fetchone()
        self.assertIn(detail_id, row[0])

    def test_omitting_detail_ids_while_removing_reference_rejected(self):
        detail_content = (
            "Full rationale and supporting evidence for the dropped reference detail claim, "
            "kept as a separate linked memory so the core itself stays short."
        )
        detail_res = self._store_normal("Dropped Reference Detail", content=detail_content)
        detail_id = detail_res["data"]["id"]
        core_res = self._store_core(
            "Core Dropping Detail Reference",
            content=f"See Dropped Reference Detail ({detail_id}): {detail_content}",
            detail_memory_ids=[detail_id],
        )
        core_id = core_res["data"]["id"]

        rejected = store_memory(
            entity_id=core_id,
            title="Core Dropping Detail Reference",
            content="Entirely rewritten body that never mentions the old detail at all.",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["errors"][0]["code"], "IMMUTABLE_MEMORY")
        # Zero side effects: the stale declaration/content must not have been persisted.
        row = self.conn.execute(
            "SELECT full_content, core_detail_memory_ids FROM entities WHERE id = ?", (core_id,)
        ).fetchone()
        self.assertIn(detail_id, row[0])
        self.assertIn(detail_id, row[1])

    def test_detail_turned_private_between_preflight_and_commit_rejects_transactionally(self):
        detail_content = (
            "Full rationale and supporting evidence for the detail later made private, kept as "
            "a separate linked memory so the core itself stays short."
        )
        detail_res = self._store_normal("Detail Later Made Private", content=detail_content)
        detail_id = detail_res["data"]["id"]
        core_res = self._store_core(
            "Core With Detail That Turns Private",
            content=f"See Detail Later Made Private ({detail_id}): {detail_content}",
            detail_memory_ids=[detail_id],
        )
        core_id = core_res["data"]["id"]

        # Simulate the detail changing state concurrently, out from under the omitted
        # detail_memory_ids preservation -- must be caught by the AUTHORITATIVE in-transaction
        # revalidation (_store_raw_entity), not just the advisory pre-check.
        self.conn.execute("UPDATE entities SET scope = 'private' WHERE id = ?", (detail_id,))

        rejected = store_memory(
            entity_id=core_id,
            title="Core With Detail That Turns Private",
            content=f"See Detail Later Made Private ({detail_id}): {detail_content}",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertTrue(rejected.startswith("Error"), rejected)

    def test_manage_relation_cannot_create_elaborates_on_into_core_directly(self):
        from saltmdb.domain.services.relation_service import store_relation

        core_res = self._store_core("Manage Relation Guard Core")
        core_id = core_res["data"]["id"]
        detail_res = self._store_normal("Manage Relation Guard Detail")
        detail_id = detail_res["data"]["id"]

        res = store_relation(
            source_id=detail_id,
            target_id=core_id,
            predicate="elaborates_on",
            owner_id="tester",
            db_connection=self.conn,
        )
        self.assertTrue(res.startswith("Error"), res)
        self.assertIn("REJECT_CORE_ELABORATES_ON", res)


class TestPreservedLifecycleFieldsRevalidated(CoreGovernanceDbTestBase):
    """Follow-up review finding #3: `detail_memory_ids=None` already meant "preserve, but still
    revalidate the effective value" (resolved review finding #4) -- `core_reason`/
    `core_exit_condition`/`core_review_after` must get the identical treatment. Omission means
    "preserve the value," never "skip validation of the effective value.\" """

    def _corrupt_reason(self, entity_id, value):
        self.conn.execute("UPDATE entities SET core_reason = ? WHERE id = ?", (value, entity_id))

    def _corrupt_exit_condition(self, entity_id, value):
        self.conn.execute(
            "UPDATE entities SET core_exit_condition = ? WHERE id = ?", (value, entity_id)
        )

    def _corrupt_review_after(self, entity_id, value):
        self.conn.execute(
            "UPDATE entities SET core_review_after = ? WHERE id = ?", (value, entity_id)
        )

    def test_preserved_too_short_reason_rejected(self):
        res = self._store_core("Malformed Preserved Reason Short")
        core_id = res["data"]["id"]
        self._corrupt_reason(core_id, "short")

        rejected = store_memory(
            entity_id=core_id,
            title="Malformed Preserved Reason Short",
            content="Distinct fixture content body for Malformed Preserved Reason Short, not a near-duplicate.",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertTrue(rejected.startswith("Error"), rejected)
        self.assertIn("core_reason", rejected)
        # Zero side effects: the stale content/reason must not have been persisted.
        row = self.conn.execute(
            "SELECT full_content, core_reason FROM entities WHERE id = ?", (core_id,)
        ).fetchone()
        self.assertEqual(row[1], "short")
        self.assertEqual(
            row[0],
            "Distinct fixture content body for Malformed Preserved Reason Short, not a near-duplicate.",
        )

    def test_preserved_too_long_reason_rejected(self):
        res = self._store_core("Malformed Preserved Reason Long")
        core_id = res["data"]["id"]
        self._corrupt_reason(core_id, "A" * (CORE_REASON_MAX_CHARS + 1))

        rejected = store_memory(
            entity_id=core_id,
            title="Malformed Preserved Reason Long",
            content="Distinct fixture content body for Malformed Preserved Reason Long, not a near-duplicate.",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertTrue(rejected.startswith("Error"), rejected)
        self.assertIn("core_reason", rejected)

    def test_preserved_invalid_exit_condition_rejected(self):
        res = self._store_core("Malformed Preserved Exit Condition")
        core_id = res["data"]["id"]
        self._corrupt_exit_condition(core_id, "short")

        rejected = store_memory(
            entity_id=core_id,
            title="Malformed Preserved Exit Condition",
            content="Distinct fixture content body for Malformed Preserved Exit Condition, not a near-duplicate.",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertTrue(rejected.startswith("Error"), rejected)
        self.assertIn("core_exit_condition", rejected)
        row = self.conn.execute(
            "SELECT core_exit_condition FROM entities WHERE id = ?", (core_id,)
        ).fetchone()
        self.assertEqual(row[0], "short")

    def test_preserved_malformed_review_after_rejected(self):
        res = self._store_core("Malformed Preserved Review After")
        core_id = res["data"]["id"]
        self._corrupt_review_after(core_id, "not-a-timestamp")

        rejected = store_memory(
            entity_id=core_id,
            title="Malformed Preserved Review After",
            content="Distinct fixture content body for Malformed Preserved Review After, not a near-duplicate.",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertTrue(rejected.startswith("Error"), rejected)
        self.assertIn("core_review_after", rejected)
        row = self.conn.execute(
            "SELECT core_review_after FROM entities WHERE id = ?", (core_id,)
        ).fetchone()
        self.assertEqual(row[0], "not-a-timestamp")

    def test_preserved_overdue_but_valid_review_after_kept_during_shrink(self):
        # A structurally valid timestamp that happens to be overdue is NOT malformed -- it may
        # still be preserved on a non-expanding (shrinking) repair; "future-only" is an admission
        # rule for SETTING a new review date, not a reason to block cleanup of an already-overdue
        # core.
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        res = self._store_core(
            "Overdue Valid Review After Preserved",
            content="A reasonably long content body that will be shortened below.",
        )
        core_id = res["data"]["id"]
        self._corrupt_review_after(core_id, past)

        allowed = store_memory(
            entity_id=core_id,
            title="Overdue Valid Review After Preserved",
            content="A reasonably long content body that will be shortened below.",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertEqual(allowed["status"], "ok")
        row = self.conn.execute(
            "SELECT core_review_after FROM entities WHERE id = ?", (core_id,)
        ).fetchone()
        self.assertEqual(row[0], past)

    def test_supplying_corrected_reason_with_non_expanding_repair_succeeds(self):
        res = self._store_core("Corrected Reason Non Expanding Repair")
        core_id = res["data"]["id"]
        self._corrupt_reason(core_id, "short")

        repaired = store_memory(
            entity_id=core_id,
            title="Corrected Reason Non Expanding Repair",
            content="Distinct fixture content body for Corrected Reason Non Expanding Repair, not a near-duplicate.",
            owner_id="tester",
            is_core=True,
            core_reason="A freshly supplied, valid core reason that repairs the corrupted value.",
            db_connection=self.conn,
        )
        self.assertEqual(repaired["status"], "ok")
        row = self.conn.execute(
            "SELECT core_reason FROM entities WHERE id = ?", (core_id,)
        ).fetchone()
        self.assertEqual(
            row[0], "A freshly supplied, valid core reason that repairs the corrupted value."
        )


class TestOverdueBoundary(CoreGovernanceDbTestBase):
    def _make_overdue(self, entity_id):
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        self.conn.execute(
            "UPDATE entities SET core_review_after = ? WHERE id = ?", (past, entity_id)
        )

    def test_new_core_blocked_while_another_is_overdue(self):
        res = self._store_core("Overdue Blocker Core")
        overdue_id = res["data"]["id"]
        self._make_overdue(overdue_id)

        blocked = self._store_core("New Core While Overdue")
        self.assertTrue(blocked.startswith("Error"), blocked)
        self.assertIn("overdue", blocked)

    def test_demote_and_archive_allowed_while_overdue(self):
        from saltmdb.domain.services.memory_service import archive_memory

        res = self._store_core("Overdue Self Archive Core")
        overdue_id = res["data"]["id"]
        self._make_overdue(overdue_id)

        archived = archive_memory(entity_id=overdue_id, db_connection=self.conn)
        self.assertIn("successfully archived", archived)

    def test_content_enlargement_blocked_while_overdue(self):
        # Resolved review finding #1: the proposed content must be varied, quality-valid prose --
        # a repeated-character enlargement (the original version of this test) is rejected by the
        # generic entropy quality gate before governance is ever consulted, making that version a
        # false positive that would pass even with the finding #1 bug still present.
        res = self._store_core(
            "Overdue Enlarge Target", content="Short but quality-gate-legal content body."
        )
        overdue_id = res["data"]["id"]
        self._make_overdue(overdue_id)

        blocked = store_memory(
            entity_id=overdue_id,
            title="Overdue Enlarge Target",
            content=(
                "This replacement body is deliberately longer and varied prose, not a repeated "
                "character run, so it clears the quality gate and exercises the overdue-boundary "
                "check on its own enlargement, not some unrelated rejection reason."
            ),
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertEqual(blocked["status"], "rejected")
        self.assertEqual(blocked["errors"][0]["code"], "IMMUTABLE_MEMORY")

    def test_shrink_of_sole_overdue_core_succeeds(self):
        res = self._store_core(
            "Overdue Self Shrink Target",
            content="A reasonably long content body that will be shortened below.",
        )
        overdue_id = res["data"]["id"]
        self._make_overdue(overdue_id)

        allowed = store_memory(
            entity_id=overdue_id,
            title="Overdue Self Shrink Target",
            content="A reasonably long content body that will be shortened below.",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertEqual(allowed["status"], "ok")

    def test_non_expanding_edit_allowed_while_another_core_is_overdue(self):
        # Both cores must exist BEFORE either is made overdue -- creating a brand new core while
        # another is already overdue is itself correctly blocked (test_new_core_blocked_while_
        # another_is_overdue), so that ordering would never reach the case under test here.
        overdue_res = self._store_core("Other Overdue Core")
        overdue_id = overdue_res["data"]["id"]
        target_res = self._store_core(
            "Non Expanding Edit Target",
            content="A reasonably long content body here that will be shortened.",
        )
        target_id = target_res["data"]["id"]
        self._make_overdue(overdue_id)

        allowed = store_memory(
            entity_id=target_id,
            title="Non Expanding Edit Target",
            content="A reasonably long content body here that will be shortened.",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertEqual(allowed["status"], "ok")

    def test_title_only_rendered_growth_of_overdue_core_rejected(self):
        # Follow-up review finding #1: enforce_overdue_boundary must compare the complete
        # canonical rendered contribution (title/core_reason/core_exit_condition/content), not
        # `full_content` length alone -- a title-only enlargement of the sole overdue core must
        # reject exactly like a content enlargement does.
        res = self._store_core(
            "Short Title", content="A reasonably long content body that stays exactly this size."
        )
        overdue_id = res["data"]["id"]
        self._make_overdue(overdue_id)

        blocked = store_memory(
            entity_id=overdue_id,
            title="A Much Longer Replacement Title That Expands The Rendered Digest Slightly",
            content="A reasonably long content body that stays exactly this size.",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertEqual(blocked["status"], "rejected")
        self.assertEqual(blocked["errors"][0]["code"], "IMMUTABLE_MEMORY")
        self.assertIn("title", blocked["errors"][0]["message"])

    def test_core_reason_only_growth_of_overdue_core_rejected(self):
        res = self._store_core(
            "Reason Growth Target", content="A reasonably long content body that stays fixed."
        )
        overdue_id = res["data"]["id"]
        self._make_overdue(overdue_id)

        blocked = store_memory(
            entity_id=overdue_id,
            title="Reason Growth Target",
            content="A reasonably long content body that stays fixed.",
            owner_id="tester",
            is_core=True,
            core_reason="A" * (CORE_REASON_MIN_CHARS + 40),
            db_connection=self.conn,
        )
        self.assertTrue(blocked.startswith("Error"), blocked)
        self.assertIn("overdue", blocked)
        self.assertIn("rendered", blocked)

    def test_core_exit_condition_only_growth_of_overdue_core_rejected(self):
        res = self._store_core(
            "Exit Growth Target", content="A reasonably long content body that stays fixed."
        )
        overdue_id = res["data"]["id"]
        self._make_overdue(overdue_id)

        blocked = store_memory(
            entity_id=overdue_id,
            title="Exit Growth Target",
            content="A reasonably long content body that stays fixed.",
            owner_id="tester",
            is_core=True,
            core_exit_condition="B" * (CORE_EXIT_MIN_CHARS + 40),
            db_connection=self.conn,
        )
        self.assertTrue(blocked.startswith("Error"), blocked)
        self.assertIn("overdue", blocked)
        self.assertIn("rendered", blocked)

    def test_equal_rendered_contribution_succeeds_while_overdue(self):
        # Same rendered length, different characters (a different core_reason of identical
        # length) -- proves the boundary is a size comparison, not a content-identity check.
        res = self._store_core(
            "Equal Rendered Target", content="A reasonably long content body that stays fixed."
        )
        overdue_id = res["data"]["id"]
        self._make_overdue(overdue_id)

        self.assertEqual(len(REASON), CORE_REASON_MIN_CHARS)
        allowed = store_memory(
            entity_id=overdue_id,
            title="Equal Rendered Target",
            content="A reasonably long content body that stays fixed.",
            owner_id="tester",
            is_core=True,
            core_reason="Z" * CORE_REASON_MIN_CHARS,
            db_connection=self.conn,
        )
        self.assertEqual(allowed["status"], "ok")

    def test_rendered_delta_decided_by_total_not_isolated_field(self):
        # A longer title fully offset by enough content shrinkage must succeed -- the boundary
        # compares the TOTAL rendered delta, never any single field's delta in isolation.
        res = self._store_core(
            "Short",
            content=(
                "This starting content body is deliberately long and varied prose so it can be "
                "shortened substantially while still clearing the quality gate on the update."
            ),
        )
        overdue_id = res["data"]["id"]
        self._make_overdue(overdue_id)

        allowed = store_memory(
            entity_id=overdue_id,
            title="A Noticeably Longer Title",
            content="Much shorter but still quality-legal replacement body.",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertEqual(allowed["status"], "rejected")
        self.assertEqual(allowed["errors"][0]["code"], "IMMUTABLE_MEMORY")

    def test_other_overdue_core_permits_only_non_increasing_rendered_edits_to_target(self):
        # Companion to test_non_expanding_edit_allowed_while_another_core_is_overdue: while some
        # OTHER core is overdue, an edit to a DIFFERENT, non-overdue target core is still bound
        # by the same rendered-delta rule (not just a content-length rule).
        overdue_res = self._store_core("Other Overdue Core Blocker")
        overdue_id = overdue_res["data"]["id"]
        target_res = self._store_core(
            "Rendered Growth Target While Other Overdue",
            content="A reasonably long content body that stays exactly this size.",
        )
        target_id = target_res["data"]["id"]
        self._make_overdue(overdue_id)

        blocked = store_memory(
            entity_id=target_id,
            title=(
                "A Much Longer Replacement Title That Expands The Rendered Digest While Another "
                "Core Is Overdue"
            ),
            content="A reasonably long content body that stays exactly this size.",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertEqual(blocked["status"], "rejected")
        self.assertEqual(blocked["errors"][0]["code"], "IMMUTABLE_MEMORY")

    def test_non_expanding_edit_allowed_while_overdue(self):
        res = self._store_core(
            "Overdue Shrink Target", content="A reasonably long content body here."
        )
        overdue_id = res["data"]["id"]
        self._make_overdue(overdue_id)

        allowed = store_memory(
            entity_id=overdue_id,
            title="Overdue Shrink Target",
            content="A reasonably long content body here.",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertEqual(allowed["status"], "ok")


class TestResolveEffectiveMemoryType(CoreGovernanceDbTestBase):
    """Unit-level coverage of `resolve_effective_memory_type` itself (third-round review
    finding): a fresh insert with an omitted type defaults to 'fact'; an update to an existing
    row with an omitted type preserves that row's own persisted type, falling back to 'fact'
    only for a legacy NULL; an explicitly supplied type always wins outright regardless of
    what the existing row holds."""

    def test_new_entity_omitted_type_defaults_to_fact(self):
        self.assertEqual(
            cgs.resolve_effective_memory_type(
                self.conn, entity_id=None, requested_memory_type=None
            ),
            "fact",
        )

    def test_new_entity_explicit_type_wins(self):
        self.assertEqual(
            cgs.resolve_effective_memory_type(
                self.conn, entity_id=None, requested_memory_type="procedure"
            ),
            "procedure",
        )

    def test_existing_entity_omitted_type_preserves_persisted_type(self):
        res = self._store_core("Effective Type Preserve Source", memory_type="decision")
        entity_id = res["data"]["id"]
        self.assertEqual(
            cgs.resolve_effective_memory_type(
                self.conn, entity_id=entity_id, requested_memory_type=None
            ),
            "decision",
        )

    def test_existing_entity_explicit_type_overrides_persisted_type(self):
        res = self._store_core("Effective Type Override Source", memory_type="decision")
        entity_id = res["data"]["id"]
        self.assertEqual(
            cgs.resolve_effective_memory_type(
                self.conn, entity_id=entity_id, requested_memory_type="event"
            ),
            "event",
        )

    def test_legacy_null_type_falls_back_to_fact(self):
        res = self._store_core("Effective Type Legacy Null Source")
        entity_id = res["data"]["id"]
        self.conn.execute("UPDATE entities SET memory_type = NULL WHERE id = ?", (entity_id,))
        self.assertEqual(
            cgs.resolve_effective_memory_type(
                self.conn, entity_id=entity_id, requested_memory_type=None
            ),
            "fact",
        )

    def test_nonexistent_explicit_entity_id_falls_back_to_fact(self):
        # An explicit entity_id that names no existing row (a fresh insert under a caller-chosen
        # id) must resolve exactly like entity_id=None -- no row to preserve a type from.
        self.assertEqual(
            cgs.resolve_effective_memory_type(
                self.conn,
                entity_id="99999999-9999-9999-9999-999999999999",
                requested_memory_type=None,
            ),
            "fact",
        )


class TestEffectiveMemoryTypeGovernanceSizing(CoreGovernanceDbTestBase):
    """Third-round review finding: both prospective-row builders sized an update with
    `memory_type or "fact"`, silently ignoring the existing row's actually-persisted type
    whenever a caller omitted `memory_type` on an update -- correct for a fresh insert, wrong
    for an update, which preserves the existing type. This under/overcounts the rendered
    bootstrap contribution used for the overdue-boundary and capacity-admission gates, letting
    some updates slip through that a byte-for-byte-accurate sizing would reject."""

    def _make_overdue(self, entity_id):
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        self.conn.execute(
            "UPDATE entities SET core_review_after = ? WHERE id = ?", (past, entity_id)
        )

    def _assert_omitted_type_overdue_growth_rejected(self, memory_type):
        # For each non-'fact' type, the false `fact` substitution renders the type string
        # shorter than the true, preserved type -- undercounting the prospective row by
        # `2 * (len(true_type) - len("fact"))` characters (the type appears twice: once in the
        # XML `type` attribute, once in the YAML `type:` line). A title growth of just 1
        # character is smaller than that undercount for every non-'fact' type, so it reproduces
        # the exact bug window: pre-fix, this growth was wrongly admitted while overdue;
        # post-fix, any positive rendered growth of the sole overdue core must reject.
        title = f"Type Preserve Overdue Target {memory_type}"
        content = f"A reasonably long content body distinct for the {memory_type} case."
        res = self._store_core(title, content=content, memory_type=memory_type)
        self.assertEqual(res["status"], "ok")
        target_id = res["data"]["id"]
        self._make_overdue(target_id)

        blocked = store_memory(
            entity_id=target_id,
            title=title + "X",  # grows the rendered title by exactly 1 character
            content=content,
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertEqual(blocked["status"], "rejected")
        self.assertEqual(blocked["errors"][0]["code"], "IMMUTABLE_MEMORY")

        # Zero side effects: the rejected title/type must not have been persisted.
        row = self.conn.execute(
            "SELECT title, memory_type FROM entities WHERE id = ?", (target_id,)
        ).fetchone()
        self.assertEqual(row[0], title)
        self.assertEqual(row[1], memory_type)

    # One test method per type (rather than a subTest loop) -- each needs its own fresh,
    # single-overdue-core database via setUp/tearDown; a shared connection across iterations
    # would leave the first iteration's own core overdue and block every later iteration's
    # creation outright (has_overdue_core, correctly, considers every active core).
    def test_omitted_type_overdue_growth_rejected_preference(self):
        self._assert_omitted_type_overdue_growth_rejected("preference")

    def test_omitted_type_overdue_growth_rejected_procedure(self):
        self._assert_omitted_type_overdue_growth_rejected("procedure")

    def test_omitted_type_overdue_growth_rejected_decision(self):
        self._assert_omitted_type_overdue_growth_rejected("decision")

    def test_omitted_type_overdue_growth_rejected_event(self):
        self._assert_omitted_type_overdue_growth_rejected("event")

    def test_omitted_type_no_rendered_growth_preserves_type_and_succeeds(self):
        # A non-expanding edit (title/content unchanged, memory_type omitted) to a non-'fact'
        # overdue core must still succeed, and must preserve the original type -- proving the
        # fix isn't simply rejecting every omitted-type update.
        res = self._store_core(
            "Non Expanding Type Preserve Target",
            content="A reasonably long content body that stays exactly this size.",
            memory_type="preference",
        )
        target_id = res["data"]["id"]
        self._make_overdue(target_id)

        allowed = store_memory(
            entity_id=target_id,
            title="Non Expanding Type Preserve Target",
            content="A reasonably long content body that stays exactly this size.",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertEqual(allowed["status"], "ok")
        row = self.conn.execute(
            "SELECT memory_type FROM entities WHERE id = ?", (target_id,)
        ).fetchone()
        self.assertEqual(row[0], "preference")

    def test_explicit_type_change_used_for_both_sizing_and_persistence(self):
        # Explicitly changing the type must use the NEW type consistently in both the overdue
        # rendered-delta comparison and the persisted row -- never the old (preserved) type and
        # never a hardcoded 'fact' default.
        res = self._store_core(
            "Explicit Type Change Target",
            content="A reasonably long content body that stays exactly this size.",
            memory_type="fact",
        )
        target_id = res["data"]["id"]
        self._make_overdue(target_id)

        # Growing the rendered contribution while ALSO explicitly changing to a longer type
        # name must be rejected using the new type's true (longer) rendered length, not the
        # old 'fact' length.
        blocked = store_memory(
            entity_id=target_id,
            title="Explicit Type Change Target",
            content="A reasonably long content body that stays exactly this size.",
            memory_type="preference",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertEqual(blocked["status"], "rejected")
        self.assertEqual(blocked["errors"][0]["code"], "IMMUTABLE_MEMORY")
        row = self.conn.execute(
            "SELECT memory_type FROM entities WHERE id = ?", (target_id,)
        ).fetchone()
        self.assertEqual(row[0], "fact")  # rejected: zero side effects, old type unchanged

        # Shrinking the body does not make an in-place type/content mutation administrative.
        allowed = store_memory(
            entity_id=target_id,
            title="Explicit Type Change Target",
            content="A shorter but still legal replacement body.",
            memory_type="preference",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertEqual(allowed["status"], "rejected")
        self.assertEqual(allowed["errors"][0]["code"], "IMMUTABLE_MEMORY")
        row = self.conn.execute(
            "SELECT memory_type FROM entities WHERE id = ?", (target_id,)
        ).fetchone()
        self.assertEqual(row[0], "fact")

    def test_omitted_type_capacity_admission_uses_preserved_type_not_fact(self):
        # Third-round review required regression #5: construct a digest just below the
        # CORE_MAX_RENDERED_CHARS cap where the false 'fact' sizing would admit the write, but
        # the true, preserved non-'fact' type pushes the exact same write over the cap.
        #
        # These filler/title/content lengths were chosen (see the fix commit) so that the total
        # rendered digest is exactly 14989 chars when the target's type is miscounted as 'fact'
        # (falsely under the 15000 cap) and exactly 15001 chars when it is correctly sized as
        # 'preference' (over the cap) -- reproducing the exact review finding.
        reason_500 = "A" * 500
        exit_500 = "B" * 500
        passage = (
            "Long-term memory governance requires careful accounting of every rendered "
            "character before a core entry is admitted into the bootstrap digest. Engineers "
            "reviewing this subsystem traced a subtle discrepancy between the row used for "
            "sizing decisions and the row actually persisted after a write commits. When a "
            "caller omits the memory type on an update, the persistence layer quietly preserves "
            "whatever type already lives on the existing row, yet the sizing preview "
            "substituted a different, shorter default instead. That mismatch let some updates "
            "slip past capacity and overdue checks that should have blocked them, because the "
            "preview believed the entry would render shorter than it actually would once "
            "committed. Correcting this required resolving the effective type once, "
            "consistently, in both the preview path and the authoritative in-transaction path, "
            "so neither could diverge from what the database would ultimately store. "
            "Regression coverage now exercises several distinct memory types to make sure no "
            "single type accidentally hides the same class of bug again."
        )

        filler_ids = []
        for i in range(CORE_MAX_ACTIVE - 1):
            title = f"Capacity Filler {i:04d}"
            self.assertEqual(len(title), 20)
            res = self._store_core(
                title,
                content=passage[i * 40 : i * 40 + 120],
                core_reason=reason_500,
                core_exit_condition=exit_500,
            )
            self.assertEqual(res["status"], "ok")
            filler_id = res["data"]["id"]
            filler_ids.append(filler_id)
            # Raw SQL, matching this suite's established `_make_overdue` pattern: pushes each
            # filler to exactly the content length the boundary math requires without routing
            # a low-entropy string back through the store-time quality gate.
            self.conn.execute(
                "UPDATE entities SET full_content = ? WHERE id = ?", ("z" * 1900, filler_id)
            )

        target_title = "Capacity Target AAAA"
        self.assertEqual(len(target_title), 20)
        target_res = self._store_core(
            target_title,
            # Disjoint from every filler's [i*40 : i*40+120] slice above (max end offset 240)
            # so this creation can never collide on the exact-content-hash duplicate guard.
            content=passage[400:520],
            core_reason=reason_500,
            core_exit_condition=exit_500,
            memory_type="preference",
        )
        self.assertEqual(target_res["status"], "ok")
        target_id = target_res["data"]["id"]

        before_row = self.conn.execute(
            "SELECT full_content, memory_type FROM entities WHERE id = ?", (target_id,)
        ).fetchone()

        update_content = "# Capacity Update\n\n" + passage[:926]
        self.assertEqual(len(update_content), 945)
        rejected = store_memory(
            entity_id=target_id,
            title=target_title,
            content=update_content,
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertIsInstance(rejected, dict)
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["errors"][0]["code"], "IMMUTABLE_MEMORY")

        # Zero side effects: the rejected update must not have touched the persisted row.
        after_row = self.conn.execute(
            "SELECT full_content, memory_type FROM entities WHERE id = ?", (target_id,)
        ).fetchone()
        self.assertEqual(before_row, after_row)

    def test_accepted_update_exact_digest_never_exceeds_prospective_admission_size(self):
        # The exact canonical digest actually rendered from the committed row must never exceed
        # the prospective digest that admitted it -- proving preview/transactional sizing and
        # the persisted row stay byte-for-byte consistent for a non-'fact' type.
        res = self._store_core(
            "Digest Consistency Target",
            content="A reasonably long content body that stays exactly this size.",
            memory_type="procedure",
        )
        target_id = res["data"]["id"]

        allowed = store_memory(
            entity_id=target_id,
            title="Digest Consistency Target",
            content="A reasonably long content body that stays exactly this size.",
            owner_id="tester",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertEqual(allowed["status"], "ok")

        committed_rows = cgs.load_active_cores(self.conn)
        committed_row = next(r for r in committed_rows if r["id"] == target_id)
        self.assertEqual(committed_row["memory_type"], "procedure")
        exact_digest_len = len(cgs.render_core_entry(committed_row, due=False))

        prospective_entry = {
            "id": target_id,
            "title": "Digest Consistency Target",
            "memory_type": cgs.resolve_effective_memory_type(
                self.conn, entity_id=target_id, requested_memory_type=None
            ),
            "core_reason": committed_row["core_reason"],
            "core_exit_condition": committed_row["core_exit_condition"],
            "core_review_after": committed_row["core_review_after"],
            "full_content": "A reasonably long content body that stays exactly this size.",
        }
        prospective_digest_len = len(cgs.render_core_entry(prospective_entry, due=False))
        self.assertLessEqual(exact_digest_len, prospective_digest_len)
        self.assertEqual(exact_digest_len, prospective_digest_len)


class TestBootstrapDigestFailClosed(CoreGovernanceDbTestBase):
    def test_empty_digest_when_no_cores(self):
        digest = cgs.render_bootstrap_response(self.conn)
        self.assertIn("<saltmdb-digest>", digest)
        self.assertIn("</saltmdb-digest>", digest)
        self.assertNotIn("<core-rules>", digest)
        self.assertNotIn("<core-bootstrap-error>", digest)

    def test_valid_cores_render_core_rules(self):
        self._store_core("Digest Core One")
        digest = cgs.render_bootstrap_response(self.conn)
        self.assertIn("<core-rules>", digest)
        self.assertIn("Digest Core One", digest)

    def test_malformed_active_core_fails_closed(self):
        marker = "UNIQUE_FULL_CONTENT_MARKER_MUST_NOT_LEAK"
        res = self._store_core("Digest Core Malformed", content=f"{marker} body text here.")
        entity_id = res["data"]["id"]
        # Corrupt the row directly to simulate an invariant violation the write path itself
        # would never produce -- exactly the "any later invalid state is corruption" scenario
        # bootstrap must fail closed on.
        self.conn.execute("UPDATE entities SET core_reason = NULL WHERE id = ?", (entity_id,))
        digest = cgs.render_bootstrap_response(self.conn)
        self.assertIn("<core-bootstrap-error>", digest)
        self.assertNotIn("<core-rules>", digest)
        # The compact inventory legitimately includes the title (plan rule 18/50), but never
        # the memory's full_content body -- that's the actual "no partial leakage" invariant.
        self.assertIn("Digest Core Malformed", digest)
        self.assertNotIn(marker, digest)

    def test_overdue_core_alone_does_not_fail_closed(self):
        res = self._store_core("Digest Overdue Core")
        entity_id = res["data"]["id"]
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        self.conn.execute(
            "UPDATE entities SET core_review_after = ? WHERE id = ?", (past, entity_id)
        )
        digest = cgs.render_bootstrap_response(self.conn)
        self.assertIn("<core-rules>", digest)
        self.assertIn('review_due="true"', digest)

    def test_overdue_core_ordered_first(self):
        self._store_core("Not Overdue Core", content="First core's body content text.")
        r2 = self._store_core("Overdue Core", content="Second core's body content text.")
        overdue_id = r2["data"]["id"]
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        self.conn.execute(
            "UPDATE entities SET core_review_after = ? WHERE id = ?", (past, overdue_id)
        )
        digest = cgs.render_bootstrap_response(self.conn)
        self.assertLess(digest.index("Overdue Core"), digest.index("Not Overdue Core"))


class TestBootstrapDetailIdInvariants(CoreGovernanceDbTestBase):
    """Resolved review finding #5: bootstrap never loaded/validated `core_detail_memory_ids` at
    all, so a corrupted or now-invalid declaration would be injected as if valid. These write
    directly to `core_detail_memory_ids` (bypassing store_memory's own write-time validation, the
    same way TestBootstrapDigestFailClosed corrupts other columns) to simulate declaration state
    the write path itself would never produce but bootstrap must still fail closed on."""

    def _corrupt_detail_ids(self, entity_id, raw_value):
        self.conn.execute(
            "UPDATE entities SET core_detail_memory_ids = ? WHERE id = ?", (raw_value, entity_id)
        )

    def test_malformed_json_fails_closed(self):
        res = self._store_core("Core Malformed Detail JSON")
        entity_id = res["data"]["id"]
        self._corrupt_detail_ids(entity_id, "{not valid json")
        digest = cgs.render_bootstrap_response(self.conn)
        self.assertIn("<core-bootstrap-error>", digest)
        self.assertNotIn("<core-rules>", digest)

    def test_too_many_detail_ids_fails_closed(self):
        res = self._store_core("Core Too Many Detail Ids")
        entity_id = res["data"]["id"]
        fake_ids = [f"1111111{i}-1111-1111-1111-111111111111" for i in range(4)]
        self._corrupt_detail_ids(entity_id, json.dumps(fake_ids))
        digest = cgs.render_bootstrap_response(self.conn)
        self.assertIn("<core-bootstrap-error>", digest)

    def test_non_uuid_detail_id_fails_closed(self):
        res = self._store_core("Core Bad Detail Id Shape")
        entity_id = res["data"]["id"]
        self._corrupt_detail_ids(entity_id, json.dumps(["not-a-uuid"]))
        digest = cgs.render_bootstrap_response(self.conn)
        self.assertIn("<core-bootstrap-error>", digest)

    def test_missing_referenced_entity_fails_closed(self):
        res = self._store_core("Core Missing Detail Entity")
        entity_id = res["data"]["id"]
        fake_uuid = "44444444-4444-4444-4444-444444444444"
        self._corrupt_detail_ids(entity_id, json.dumps([fake_uuid]))
        digest = cgs.render_bootstrap_response(self.conn)
        self.assertIn("<core-bootstrap-error>", digest)

    def test_declared_detail_promoted_to_core_fails_closed(self):
        # A core cannot declare another core as a detail -- valid at write time, but the detail
        # can be independently promoted to core afterward.
        detail_content = (
            "Full rationale and supporting evidence for the detail later promoted to core, kept "
            "as a separate linked memory so the core itself stays short."
        )
        detail_res = self._store_normal("Detail Later Promoted To Core", content=detail_content)
        detail_id = detail_res["data"]["id"]
        core_res = self._store_core(
            "Core With Detail Later Promoted",
            content=f"See Detail Later Promoted To Core ({detail_id}): {detail_content}",
            detail_memory_ids=[detail_id],
        )
        self.assertEqual(core_res["status"], "ok")
        self.conn.execute("UPDATE entities SET is_core = 1 WHERE id = ?", (detail_id,))
        digest = cgs.render_bootstrap_response(self.conn)
        self.assertIn("<core-bootstrap-error>", digest)

    def test_declared_detail_made_private_fails_closed(self):
        detail_res = self._store_normal("Detail Later Made Private For Bootstrap")
        detail_id = detail_res["data"]["id"]
        core_res = self._store_core(
            "Core With Detail Later Made Private",
            content=(
                f"See Detail Later Made Private For Bootstrap ({detail_id}) for full rationale "
                "and evidence."
            ),
            detail_memory_ids=[detail_id],
        )
        self.assertEqual(core_res["status"], "ok")
        self.conn.execute("UPDATE entities SET scope = 'private' WHERE id = ?", (detail_id,))
        digest = cgs.render_bootstrap_response(self.conn)
        self.assertIn("<core-bootstrap-error>", digest)

    def test_declared_detail_missing_title_or_uuid_mention_fails_closed(self):
        res = self._store_core("Core Detail Mention Later Broken")
        entity_id = res["data"]["id"]
        detail_res = self._store_normal("Standalone Detail For Mention Test")
        detail_id = detail_res["data"]["id"]
        # Bypass write-time validation directly -- simulates the core's content being rewritten
        # in a way store_memory's own path would never allow (that path is covered by finding #4
        # regression tests instead), leaving a stale declaration bootstrap must catch.
        self.conn.execute(
            "UPDATE entities SET core_detail_memory_ids = ? WHERE id = ?",
            (json.dumps([detail_id]), entity_id),
        )
        digest = cgs.render_bootstrap_response(self.conn)
        self.assertIn("<core-bootstrap-error>", digest)

    def test_archived_shared_non_core_detail_remains_valid(self):
        detail_content = (
            "Full rationale and supporting evidence for the archived valid detail claim, kept "
            "as a separate linked memory so the core itself stays short."
        )
        detail_res = self._store_normal("Archived Valid Detail", content=detail_content)
        detail_id = detail_res["data"]["id"]
        core_res = self._store_core(
            "Core With Archived Detail",
            content=f"See Archived Valid Detail ({detail_id}): {detail_content}",
            detail_memory_ids=[detail_id],
        )
        self.assertEqual(core_res["status"], "ok")
        self.conn.execute("UPDATE entities SET status = 'archived' WHERE id = ?", (detail_id,))

        digest = cgs.render_bootstrap_response(self.conn)
        self.assertIn("<core-rules>", digest)
        self.assertNotIn("<core-bootstrap-error>", digest)

    def test_bootstrap_error_never_leaks_full_content_for_detail_violations(self):
        marker = "UNIQUE_DETAIL_VIOLATION_MARKER_MUST_NOT_LEAK"
        res = self._store_core(
            "Core Detail Violation Content Leak Check", content=f"{marker} body."
        )
        entity_id = res["data"]["id"]
        self._corrupt_detail_ids(entity_id, "{not valid json")
        digest = cgs.render_bootstrap_response(self.conn)
        self.assertIn("<core-bootstrap-error>", digest)
        self.assertNotIn(marker, digest)


class TestReviewCoreMemory(CoreGovernanceDbTestBase):
    RATIONALE = "C" * CORE_REVIEW_RATIONALE_MIN_CHARS

    def test_retain_extends_review_date_without_changing_content(self):
        res = self._store_core("Retain Target Core")
        entity_id = res["data"]["id"]
        original_content = self.conn.execute(
            "SELECT full_content FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()[0]

        result = cgs.review_core_memory(
            self.conn,
            entity_id=entity_id,
            outcome="retain",
            review_rationale=self.RATIONALE,
            owner_id="reviewer_agent",
        )
        self.assertIn("retained as core", result)
        row = self.conn.execute(
            "SELECT full_content, is_core, core_last_reviewed_by FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        self.assertEqual(row[0], original_content)
        self.assertTrue(row[1])
        self.assertEqual(row[2], "reviewer_agent")

    def test_retain_requires_future_timestamp(self):
        res = self._store_core("Retain Past Timestamp Core")
        entity_id = res["data"]["id"]
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        result = cgs.review_core_memory(
            self.conn,
            entity_id=entity_id,
            outcome="retain",
            review_rationale=self.RATIONALE,
            owner_id="reviewer_agent",
            core_review_after=past,
        )
        self.assertTrue(result.startswith("Error"), result)

    def test_retain_against_non_core_rejected(self):
        res = self._store_normal("Non Core Retain Target")
        entity_id = res["data"]["id"]
        result = cgs.review_core_memory(
            self.conn,
            entity_id=entity_id,
            outcome="retain",
            review_rationale=self.RATIONALE,
            owner_id="reviewer_agent",
        )
        self.assertTrue(result.startswith("Error"), result)

    def test_demote_turns_core_into_searchable_normal_memory(self):
        res = self._store_core("Demote Target Core")
        entity_id = res["data"]["id"]

        result = cgs.review_core_memory(
            self.conn,
            entity_id=entity_id,
            outcome="demote",
            review_rationale=self.RATIONALE,
            owner_id="reviewer_agent",
        )
        self.assertIn("demoted", result)
        row = self.conn.execute(
            "SELECT is_core, status, core_review_after FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertFalse(row[0])
        self.assertEqual(row[1], "raw")
        self.assertIsNone(row[2])
        core_tag = self.conn.execute(
            """
            SELECT 1 FROM entity_tags et JOIN tags t ON t.id = et.tag_id
            WHERE et.entity_id = ? AND t.name = '#core'
            """,
            (entity_id,),
        ).fetchone()
        self.assertIsNone(core_tag)

    def test_demote_is_idempotent_noop(self):
        res = self._store_core("Double Demote Core")
        entity_id = res["data"]["id"]
        cgs.review_core_memory(
            self.conn,
            entity_id=entity_id,
            outcome="demote",
            review_rationale=self.RATIONALE,
            owner_id="reviewer_agent",
        )
        second = cgs.review_core_memory(
            self.conn,
            entity_id=entity_id,
            outcome="demote",
            review_rationale=self.RATIONALE,
            owner_id="reviewer_agent",
        )
        self.assertIn("no-op", second)

    def test_archive_retires_memory_and_preserves_exact_id_retrievability(self):
        from saltmdb.domain.services.memory_service import fetch_memory_chunk

        res = self._store_core("Archive Target Core")
        entity_id = res["data"]["id"]

        result = cgs.review_core_memory(
            self.conn,
            entity_id=entity_id,
            outcome="archive",
            review_rationale=self.RATIONALE,
            owner_id="reviewer_agent",
        )
        self.assertIn("archived", result)
        row = self.conn.execute("SELECT status FROM entities WHERE id = ?", (entity_id,)).fetchone()
        self.assertEqual(row[0], "archived")
        content = fetch_memory_chunk(entity_id=entity_id, db_connection=self.conn)
        self.assertNotIn("Error", content)

    def test_reviewer_identity_independent_of_entity_owner(self):
        res = self._store_core("Owner Mismatch Core", owner_id="original_owner")
        entity_id = res["data"]["id"]

        # archive_memory's public ownership guard would reject a mismatched owner_id; the
        # review path must NOT inherit that restriction (reviewer identity, not an ownership
        # permission).
        result = cgs.review_core_memory(
            self.conn,
            entity_id=entity_id,
            outcome="archive",
            review_rationale=self.RATIONALE,
            owner_id="a_completely_different_reviewer",
        )
        self.assertIn("archived", result)

    def test_archive_rejects_ordinary_active_non_core_memory(self):
        # Resolved review finding #3: review_core_memory(outcome='archive') must not become a
        # second, ownership-neutral general-purpose archive API for every ordinary memory.
        res = self._store_normal("Ordinary Active Memory", owner_id="alice")
        entity_id = res["data"]["id"]

        result = cgs.review_core_memory(
            self.conn,
            entity_id=entity_id,
            outcome="archive",
            review_rationale=self.RATIONALE,
            owner_id="bob",
        )
        self.assertTrue(result.startswith("Error"), result)
        self.assertIn("core memory", result)
        row = self.conn.execute("SELECT status FROM entities WHERE id = ?", (entity_id,)).fetchone()
        self.assertEqual(row[0], "raw")

    def test_archive_already_archived_former_core_is_noop(self):
        from saltmdb.domain.services.memory_service import archive_memory

        res = self._store_core("Former Core Already Archived")
        entity_id = res["data"]["id"]
        # Archive it OUTSIDE review_core_memory first (archive_memory never touches is_core, so
        # the row stays is_core=1, status='archived' -- a genuinely former core).
        archive_memory(entity_id=entity_id, db_connection=self.conn)

        result = cgs.review_core_memory(
            self.conn,
            entity_id=entity_id,
            outcome="archive",
            review_rationale=self.RATIONALE,
            owner_id="reviewer_agent",
        )
        self.assertIn("no-op", result)

    def test_archive_rejects_already_archived_never_core_memory(self):
        from saltmdb.domain.services.memory_service import archive_memory

        res = self._store_normal("Never Core Already Archived")
        entity_id = res["data"]["id"]
        archive_memory(entity_id=entity_id, db_connection=self.conn)

        result = cgs.review_core_memory(
            self.conn,
            entity_id=entity_id,
            outcome="archive",
            review_rationale=self.RATIONALE,
            owner_id="reviewer_agent",
        )
        # Must not masquerade as a reviewed core's no-op -- a never-core memory gets the
        # rejection, not the archived-former-core no-op message.
        self.assertTrue(result.startswith("Error"), result)
        self.assertIn("core memory", result)

    def test_archive_via_review_does_not_weaken_public_archive_memory_guard(self):
        from saltmdb.domain.services.memory_service import archive_memory

        res = self._store_normal("Public Guard Untouched Entity", owner_id="original_owner")
        entity_id = res["data"]["id"]
        result = archive_memory(
            entity_id=entity_id, owner_id="someone_else", db_connection=self.conn
        )
        self.assertIn("owner mismatch", result)

    def test_demote_and_archive_reject_supplied_core_review_after(self):
        res = self._store_core("Reject Review After Core")
        entity_id = res["data"]["id"]
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        result = cgs.review_core_memory(
            self.conn,
            entity_id=entity_id,
            outcome="demote",
            review_rationale=self.RATIONALE,
            owner_id="reviewer_agent",
            core_review_after=future,
        )
        self.assertTrue(result.startswith("Error"), result)

    def test_invalid_outcome_rejected(self):
        res = self._store_core("Invalid Outcome Core")
        entity_id = res["data"]["id"]
        result = cgs.review_core_memory(
            self.conn,
            entity_id=entity_id,
            outcome="bogus",
            review_rationale=self.RATIONALE,
            owner_id="reviewer_agent",
        )
        self.assertTrue(result.startswith("Error"), result)


if __name__ == "__main__":
    unittest.main()
