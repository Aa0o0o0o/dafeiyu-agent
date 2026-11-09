"""Fake-only diagnostic stages; no real process, DB, config or signature."""
import importlib.util
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from test_controlled_probe import FakeProbeAPI
from test_metadata_review import PATH
from test_probe import FakeReader, synthetic_case

FILE = Path(__file__).resolve().parents[2] / ".tmp" / "wechat_probe_stages_20261101.py"
spec = importlib.util.spec_from_file_location("synthetic_stage_diagnostic", FILE)
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


class StagesTests(unittest.TestCase):
    def test_synthetic_match_and_hmac_failure(self):
        block, page = synthetic_case()
        self.assertEqual(diagnostic.scan_one_time(FakeReader(block, ""), page), "MATCH")
        bad = page[:100] + bytes([page[100] ^ 1]) + page[101:]
        self.assertEqual(diagnostic.scan_one_time(FakeReader(block, ""), bad), "CONFIG_CANDIDATE_NOT_VERIFIED")

    def test_marker_and_reference_stages(self):
        block, page = synthetic_case()
        self.assertEqual(diagnostic.scan_one_time(FakeReader(b"x" * len(block), ""), page), "MARKER_NOT_VERIFIED")
        sample = bytearray(len(block))
        sample[0x100:0x100 + len(diagnostic.probe._NAME)] = diagnostic.probe._NAME
        self.assertEqual(diagnostic.scan_one_time(FakeReader(bytes(sample), ""), page), "CONFIG_REFERENCE_NOT_VERIFIED")

    def test_inaccessible_and_timeout_are_not_absence(self):
        class Inaccessible(FakeReader):
            def read(self, address, size):
                return None
        class Timeout(FakeReader):
            def read(self, address, size):
                raise TimeoutError("synthetic")
        self.assertEqual(diagnostic.scan_one_time(Inaccessible(b"x" * 256, ""), b"p" * 4096), "SCAN_INCOMPLETE")
        self.assertEqual(diagnostic.scan_one_time(Timeout(b"x" * 256, ""), b"p" * 4096), "SCAN_LIMIT")

    def test_one_operation_reuses_fake_handle_and_prints_nothing(self):
        block, page = synthetic_case()
        fake = FakeProbeAPI()
        fake.real_synthetic_page = page
        borrowed = []
        def factory(adapter, handle):
            borrowed.append(handle)
            return FakeReader(block, "")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = diagnostic.decide(["12"], discover=lambda: PATH, signer=lambda: "A" * 40,
                                       api_factory=lambda: fake, reader_factory=factory)
        self.assertEqual(result, "MATCH")
        self.assertEqual(borrowed, ["process"])
        self.assertEqual(fake.closed, list(reversed(fake.handles)))
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_after_check_change_discards_match_and_failures_are_fixed(self):
        block, page = synthetic_case()
        fake = FakeProbeAPI()
        fake.real_synthetic_page = page
        fake.file_change = PATH
        result = diagnostic.decide(["12"], discover=lambda: PATH, signer=lambda: "A" * 40,
                                   api_factory=lambda: fake, reader_factory=lambda *_: FakeReader(block, ""))
        self.assertEqual(result, "CONTACT_METADATA_NOT_VERIFIED")
        self.assertEqual(diagnostic.decide(["12"], discover=lambda: None), "DISCOVERY_NOT_VERIFIED")
        self.assertEqual(diagnostic.decide(["12"], discover=lambda: PATH, signer=lambda: None), "SIGNATURE_NOT_VERIFIED")
        self.assertEqual(diagnostic.decide(["bad"]), "INPUT_INVALID")

    def test_unavailable_page_does_not_scan(self):
        fake = FakeProbeAPI()
        fake.short_page = True
        def never(*args):
            self.fail("unexpected scan")
        result = diagnostic.decide(["12"], discover=lambda: PATH, signer=lambda: "A" * 40,
                                   api_factory=lambda: fake, reader_factory=never)
        self.assertEqual(result, "PAGE_NOT_VERIFIED")


if __name__ == "__main__":
    unittest.main()
