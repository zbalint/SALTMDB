import sys
import unittest
from unittest.mock import patch


class TestProcessMemory(unittest.TestCase):
    def test_proc_status_converts_kilobytes_to_bytes(self):
        from saltmdb.utils.process_memory import _parse_proc_status

        self.assertEqual(
            _parse_proc_status("Name:\tpython\nVmRSS:\t  204800 kB\nVmHWM:\t  307200 kB\n"),
            (209715200.0, 314572800.0),
        )

    def test_proc_status_returns_none_when_required_fields_are_missing(self):
        from saltmdb.utils.process_memory import _parse_proc_status

        self.assertIsNone(_parse_proc_status("VmRSS:\t  204800 kB\n"))
        self.assertIsNone(_parse_proc_status(""))

    def test_format_process_memory_converts_bytes_to_mebibytes(self):
        from saltmdb.utils import process_memory

        with patch.object(
            process_memory,
            "read_process_memory",
            return_value=(209715200.0, 314572800.0),
        ):
            self.assertEqual(
                process_memory.format_process_memory(),
                "rss_mb=200.0 peak_rss_mb=300.0",
            )

    def test_format_process_memory_reports_unavailable_read(self):
        from saltmdb.utils import process_memory

        with patch.object(process_memory, "read_process_memory", return_value=None):
            self.assertEqual(
                process_memory.format_process_memory(),
                "rss_mb=unavailable peak_rss_mb=unavailable",
            )

    def test_format_process_memory_contains_read_exception(self):
        from saltmdb.utils import process_memory

        with patch.object(
            process_memory,
            "read_process_memory",
            side_effect=OSError("synthetic read failure"),
        ):
            self.assertEqual(
                process_memory.format_process_memory(),
                "rss_mb=unavailable peak_rss_mb=unavailable",
            )

    def test_unsupported_platform_returns_none(self):
        from saltmdb.utils import process_memory

        with patch.object(process_memory.sys, "platform", "darwin"):
            self.assertIsNone(process_memory.read_process_memory())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux-only process memory read")
    def test_real_linux_process_memory_read(self):
        from saltmdb.utils.process_memory import read_process_memory

        result = read_process_memory()
        if result is None:
            self.fail("Linux process-memory read was unavailable")
        rss_bytes, peak_bytes = result
        self.assertGreater(rss_bytes, 0)
        self.assertGreaterEqual(peak_bytes, rss_bytes)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only process memory read")
    def test_real_windows_process_memory_read(self):
        from saltmdb.utils.process_memory import _PROCESS_MEMORY_COUNTERS, read_process_memory
        import ctypes

        self.assertEqual(ctypes.sizeof(_PROCESS_MEMORY_COUNTERS), 72)
        result = read_process_memory()
        if result is None:
            self.fail("Windows process-memory read was unavailable")
        rss_bytes, peak_bytes = result
        self.assertGreater(rss_bytes, 0)
        self.assertGreaterEqual(peak_bytes, rss_bytes)
