"""Metadata-only diagnostic tests with fake adapters and no real discovery."""
import importlib.util
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import probe
from test_metadata_review import FakeAPI, PATH

FILE = Path(__file__).resolve().parents[2] / ".tmp" / "wechat_metadata_diagnostic_20261101.py"
spec = importlib.util.spec_from_file_location("synthetic_metadata_diagnostic", FILE)
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


def decide(fake, *, discover=lambda: PATH, signer=lambda: "A" * 40):
    return diagnostic.decide(["12"], discover=discover, signer=signer,
                             api_factory=lambda: fake)


class DiagnosticTests(unittest.TestCase):
    def test_invalid_input_and_initialization_fail_closed(self):
        self.assertEqual(diagnostic.decide([]), "INPUT_INVALID")
        self.assertEqual(diagnostic.decide(["bad"]), "INPUT_INVALID")
        with patch.object(diagnostic, "MODULES_READY", False):
            self.assertEqual(diagnostic.decide(["12"]), "INIT_UNAVAILABLE")

    def test_discovery_and_signature_are_distinct(self):
        fake = FakeAPI()
        self.assertEqual(decide(fake, discover=lambda: None), "DISCOVERY_NOT_VERIFIED")
        self.assertEqual(decide(fake, signer=lambda: None), "SIGNATURE_NOT_VERIFIED")
        self.assertEqual(fake.handles, [])

    def test_window_process_and_file_failures_have_fixed_stages(self):
        for field, value, status in (
            ("window_change", True, "WINDOW_NOT_VERIFIED"),
            ("wrong_image", True, "PROCESS_NOT_VERIFIED"),
            ("bad_reparse", PATH, "CONTACT_METADATA_NOT_VERIFIED"),
        ):
            fake = FakeAPI()
            setattr(fake, field, value)
            self.assertEqual(decide(fake), status)
            self.assertEqual(fake.closed, list(reversed(fake.handles)))

    def test_ready_is_metadata_only_and_never_scans(self):
        fake = FakeAPI()
        out, err = io.StringIO(), io.StringIO()
        with patch.object(probe, "_scan_for_page_match", side_effect=AssertionError("must not scan")),              redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(decide(fake), "METADATA_READY_NO_SCAN")
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.assertEqual(fake.closed, list(reversed(fake.handles)))

    def test_no_detail_from_api_exception(self):
        class ErrorAPI(FakeAPI):
            def open_metadata(self, path):
                raise RuntimeError("synthetic secret path")
        self.assertEqual(decide(ErrorAPI()), "CONTACT_METADATA_NOT_VERIFIED")


if __name__ == "__main__":
    unittest.main()
