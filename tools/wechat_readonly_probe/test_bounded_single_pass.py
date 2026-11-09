"""Fake-only tests for read bounds, chunk boundaries and address association."""
import struct
import unittest
from unittest.mock import patch

import bounded_single_pass as scan
import probe
import synthetic_scan_comparison as comparison
from test_probe import synthetic_case
from test_synthetic_scan_comparison import relocate


class ScannerTests(unittest.TestCase):
    def test_both_orders_cross_chunks_and_regions(self):
        for marker, pair in ((0x17B, 0x3F4), (0x3FA, 0x174), (0x3FA, 0x874), (0x8FA, 0x374)):
            original, page = synthetic_case()
            block = relocate(original, marker, pair)
            regions = ((0x10000, block[:0x800]), (0x10800, block[0x800:]))
            fake = comparison.SyntheticMemory(regions, budget=17000, chunk=256)
            self.assertEqual(scan.scan(fake, page, chunk_size=256), "MATCH")
            self.assertLessEqual(fake.bytes_read, 17000)

    def test_wrong_page_and_budget_fail_closed(self):
        block, page = synthetic_case()
        bad = page[:100] + bytes([page[100] ^ 1]) + page[101:]
        fake = comparison.SyntheticMemory(((0x10000, block),), chunk=256)
        self.assertEqual(scan.scan(fake, bad, chunk_size=256), "CONFIG_CANDIDATE_NOT_VERIFIED")
        fake = comparison.SyntheticMemory(((0x10000, block),), budget=100, chunk=256)
        self.assertEqual(scan.scan(fake, page, chunk_size=256), "SCAN_LIMIT")
        self.assertLessEqual(fake.bytes_read, 100)

    def test_no_marker_or_reference_are_not_absence_claims(self):
        fake = comparison.SyntheticMemory(((0x10000, b"x" * 1024),), chunk=128)
        self.assertEqual(scan.scan(fake, b"p" * 4096, chunk_size=128), "MARKER_NOT_VERIFIED")
        data = bytearray(b"x" * 1024)
        data[100:100 + len(probe._NAME)] = probe._NAME
        fake = comparison.SyntheticMemory(((0x10000, bytes(data)),), chunk=128)
        self.assertEqual(scan.scan(fake, b"p" * 4096, chunk_size=128), "CONFIG_REFERENCE_NOT_VERIFIED")

    def test_reference_and_marker_overflow_are_incomplete(self):
        data = struct.pack("<QQ", 0x20000, len(probe._NAME)) * 4
        fake = comparison.SyntheticMemory(((0x10000, data),), chunk=32)
        with patch.object(scan, "_MAX_REFERENCES", 2):
            self.assertEqual(scan.scan(fake, b"p" * 4096, chunk_size=32), "SCAN_INCOMPLETE")
        data = probe._NAME + b"x" * 100 + probe._NAME
        fake = comparison.SyntheticMemory(((0x10000, data),), chunk=64)
        with patch.object(scan, "_MAX_MARKERS", 1):
            self.assertEqual(scan.scan(fake, b"p" * 4096, chunk_size=64), "SCAN_INCOMPLETE")

    def test_unreadable_and_region_limits_fail_closed(self):
        class Unreadable:
            def regions(self):
                yield 0x10000, 1024
            def read(self, address, size):
                return None
        self.assertEqual(scan.scan(Unreadable(), b"p" * 4096, chunk_size=128), "SCAN_INCOMPLETE")
        fake = comparison.SyntheticMemory(((0x10000, b"x"), (0x20000, b"x")), chunk=1)
        with patch.object(scan, "_MAX_REGIONS", 1):
            self.assertEqual(scan.scan(fake, b"p" * 4096, chunk_size=1), "SCAN_INCOMPLETE")

    def test_internal_consumer_cannot_return_key_as_status(self):
        block, page = synthetic_case()
        fake = comparison.SyntheticMemory(((0x10000, block),), chunk=256)
        result = scan._scan(fake, page, chunk_size=256, consumer=lambda candidate: candidate)
        self.assertEqual(result, "SCAN_ERROR")
        class FakeEqual:
            def __eq__(self, other):
                return True
        fake = comparison.SyntheticMemory(((0x10000, block),), chunk=256)
        result = scan._scan(fake, page, chunk_size=256, consumer=lambda candidate: FakeEqual())
        self.assertEqual(result, "SCAN_ERROR")
        fake = comparison.SyntheticMemory(((0x10000, block),), chunk=256)
        result = scan._scan(fake, page, chunk_size=256, consumer=lambda candidate: "SYNTHETIC_IDENTITIES_MATCHED")
        self.assertEqual(result, "SYNTHETIC_IDENTITIES_MATCHED")
    def test_expired_deadline_fails_without_read(self):
        fake = comparison.SyntheticMemory(((0x10000, b"x" * 100),))
        fake._deadline = 0
        self.assertEqual(scan.scan(fake, b"p" * 4096), "SCAN_LIMIT")
        self.assertEqual(fake.bytes_read, 0)
    def test_input_type_and_overlap_rejected(self):
        self.assertEqual(scan.scan(object(), b"short"), "SCAN_ERROR")
        fake = comparison.SyntheticMemory(((0x10000, b"x" * 10), (0x10005, b"x" * 10)))
        self.assertEqual(scan.scan(fake, b"p" * 4096, chunk_size=8), "SCAN_INCOMPLETE")


if __name__ == "__main__":
    unittest.main()



