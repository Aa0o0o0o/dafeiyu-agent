"""Randomized synthetic memory layout and deterministic byte-budget checks."""
import struct
import unittest

import probe
import synthetic_scan_comparison as comparison
from test_probe import synthetic_case


def relocate(block, marker_offset, pair_offset):
    data = bytearray(block)
    node = bytes(data[0x2F0:0x340])
    data[0x100:0x100 + len(probe._NAME)] = bytes(len(probe._NAME))
    data[0x2F0:0x340] = bytes(0x50)
    data[pair_offset - 16:pair_offset - 16 + len(node)] = node
    struct.pack_into("<Q", data, pair_offset, 0x10000 + marker_offset)
    data[marker_offset:marker_offset + len(probe._NAME)] = probe._NAME
    return bytes(data)


class ComparisonTests(unittest.TestCase):
    def test_both_orders_and_chunk_boundaries(self):
        for marker, pair in ((0x17B, 0x3F4), (0x3FA, 0x174)):
            original, page = synthetic_case()
            block = relocate(original, marker, pair)
            for strategy in (comparison.one_pass, comparison.two_pass):
                with self.subTest(marker=marker, pair=pair, strategy=strategy.__name__):
                    memory = comparison.SyntheticMemory(((0x10000, block),), chunk=128)
                    self.assertEqual(strategy(memory, page), "MATCH")
                    self.assertLessEqual(memory.bytes_read, memory.budget)

    def test_matches_across_separate_synthetic_regions(self):
        for marker, pair in ((0x3FA, 0x874), (0x8FA, 0x374)):
            original, page = synthetic_case()
            block = relocate(original, marker, pair)
            regions = ((0x10000, block[:0x800]), (0x10800, block[0x800:]))
            for strategy in (comparison.one_pass, comparison.two_pass):
                memory = comparison.SyntheticMemory(regions, chunk=256)
                self.assertEqual(strategy(memory, page), "MATCH")
    def test_hmac_failure_never_becomes_match(self):
        block, page = synthetic_case()
        corrupted = page[:100] + bytes([page[100] ^ 1]) + page[101:]
        for strategy in (comparison.one_pass, comparison.two_pass):
            memory = comparison.SyntheticMemory(((0x10000, block),), chunk=256)
            self.assertEqual(strategy(memory, corrupted), "NOT_VERIFIED")

    def test_single_pass_saves_bytes_with_same_budget(self):
        original, page = synthetic_case()
        block = relocate(original, 0x17B, 0x3F4)
        limit = 17000
        one = comparison.SyntheticMemory(((0x10000, block),), budget=limit, chunk=256)
        two = comparison.SyntheticMemory(((0x10000, block),), budget=limit, chunk=256)
        old = comparison.SyntheticMemory(((0x10000, block),), budget=limit, chunk=256)
        self.assertEqual(comparison.one_pass(one, page), "MATCH")
        self.assertEqual(comparison.two_pass(two, page), "LIMIT")
        with self.assertRaises(TimeoutError):
            probe._scan_for_page_match(old, page)
        self.assertLess(one.bytes_read, two.bytes_read)
        self.assertTrue(all(memory.bytes_read <= limit for memory in (one, two, old)))

    def test_oversized_candidate_set_is_incomplete(self):
        item = struct.pack("<QQ", 0x20000, len(probe._NAME))
        data = item * (comparison._MAX_REFS + 2)
        memory = comparison.SyntheticMemory(((0x10000, data),), chunk=1024)
        self.assertEqual(comparison.one_pass(memory, b"x" * 4096), "INCOMPLETE")

    def test_real_or_unbounded_source_is_rejected(self):
        with self.assertRaises(ValueError):
            comparison.one_pass(object(), b"x" * 4096)
        with self.assertRaises(ValueError):
            comparison.SyntheticMemory(((0x10000, b"x" * 4194305),))


if __name__ == "__main__":
    unittest.main()

