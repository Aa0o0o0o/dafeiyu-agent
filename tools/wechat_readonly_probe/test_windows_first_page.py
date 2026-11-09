"""Actual Win32 file-pointer regression on a random synthetic file only."""
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import controlled_probe

ROOT = Path(__file__).resolve().parents[2]


class FirstPageTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows API test")
    def test_twice_reads_same_first_page_from_same_synthetic_handle(self):
        with tempfile.TemporaryDirectory(prefix="dafeiyu-page-fake-", dir=ROOT / ".tmp") as directory:
            path = Path(directory) / "synthetic.db"
            first, second = secrets.token_bytes(4096), secrets.token_bytes(4096)
            path.write_bytes(first + second)
            api = controlled_probe.WindowsProbeAPI()
            handle = api.open_target(str(path))
            try:
                self.assertEqual(api.first_page(handle), first)
                self.assertEqual(api.first_page(handle), first)
                self.assertNotEqual(first, second)
            finally:
                api.close(handle)

    def test_seek_failure_stops_before_read(self):
        api = controlled_probe.WindowsProbeAPI.__new__(controlled_probe.WindowsProbeAPI)
        read = Mock()
        api.k = SimpleNamespace(SetFilePointerEx=Mock(return_value=0), ReadFile=read)
        with self.assertRaises(RuntimeError):
            api.first_page(object())
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
