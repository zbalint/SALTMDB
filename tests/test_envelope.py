import unittest

from saltmdb.utils import envelope


class TestEnvelope(unittest.TestCase):
    def test_ok_default_shape(self):
        env = envelope.ok({"id": "abc"})
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["data"], {"id": "abc"})
        self.assertEqual(env["warnings"], [])
        self.assertEqual(env["errors"], [])
        self.assertNotIn("corrected_call", env)
        self.assertNotIn("effective", env)

    def test_ok_with_warnings_and_effective(self):
        w = envelope.warning("TAG_NEAR_MISS", "did you mean #docs?", detail={"tag": "#doc"})
        env = envelope.ok(
            {"id": "abc"},
            warnings=[w],
            effective={"agent_id": "claude", "scope": "shared"},
        )
        self.assertEqual(env["warnings"], [w])
        self.assertEqual(env["errors"], [])
        self.assertEqual(env["effective"]["agent_id"], "claude")

    def test_error_carries_one_correction_or_alternatives(self):
        corrected = envelope.error("X", "fix", corrected_call={"title": "T"})
        alternatives = envelope.error(
            "X",
            "choose",
            corrected_calls=[{"core": {}}, {"demote_core": True}],
        )
        self.assertEqual(corrected["corrected_call"], {"title": "T"})
        self.assertEqual(alternatives["corrected_calls"], [{"core": {}}, {"demote_core": True}])
        with self.assertRaises(ValueError):
            envelope.error("X", "ambiguous", corrected_call={}, corrected_calls=[{}])

    def test_rejected_requires_at_least_one_error(self):
        with self.assertRaises(ValueError):
            envelope.rejected([])

    def test_rejected_shape(self):
        e = envelope.error("MISSING_TAGS", "tags is required", field="tags")
        env = envelope.rejected([e])
        self.assertEqual(env["status"], "rejected")
        self.assertEqual(env["errors"], [e])
        self.assertEqual(env["warnings"], [])
        self.assertNotIn("data", env)

    def test_error_omits_field_when_not_whole_call(self):
        e = envelope.error("QUALITY_REJECTED", "content is a placeholder")
        self.assertNotIn("field", e)

    def test_is_ok_is_rejected_predicates(self):
        self.assertTrue(envelope.is_ok(envelope.ok(None)))
        self.assertFalse(envelope.is_rejected(envelope.ok(None)))
        rej = envelope.rejected([envelope.error("X", "x")])
        self.assertTrue(envelope.is_rejected(rej))
        self.assertFalse(envelope.is_ok(rej))
        self.assertFalse(envelope.is_ok("not a dict"))
        self.assertFalse(envelope.is_rejected(None))


if __name__ == "__main__":
    unittest.main()
