import unittest
from saltmdb.domain.services import ephemeral_service

class TestEphemeralService(unittest.TestCase):
    def test_store_and_get_roundtrip(self):
        res = ephemeral_service.store_ephemeral_memory(key="probe_key_1", value="probe_value_1")
        self.assertIn("stored successfully", res)

        val = ephemeral_service.get_ephemeral_memory(key="probe_key_1")
        self.assertEqual(val, "probe_value_1")

    def test_store_overwrites_existing_key(self):
        ephemeral_service.store_ephemeral_memory(key="probe_key_2", value="first_value")
        ephemeral_service.store_ephemeral_memory(key="probe_key_2", value="second_value")

        val = ephemeral_service.get_ephemeral_memory(key="probe_key_2")
        self.assertEqual(val, "second_value")

    def test_get_missing_key_returns_not_found_message(self):
        val = ephemeral_service.get_ephemeral_memory(key="key_that_was_never_stored")
        self.assertIn("not found", val)

    def test_store_missing_key_or_value_returns_error(self):
        res_no_key = ephemeral_service.store_ephemeral_memory(key=None, value="some_value")
        self.assertIn("Error", res_no_key)

        res_no_value = ephemeral_service.store_ephemeral_memory(key="some_key", value=None)
        self.assertIn("Error", res_no_value)

    def test_get_missing_key_returns_error(self):
        res = ephemeral_service.get_ephemeral_memory(key=None)
        self.assertIn("Error", res)

if __name__ == "__main__":
    unittest.main()
