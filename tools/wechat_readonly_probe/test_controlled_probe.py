"""Synthetic checks only; no OS process, file, QQ, key or DB access."""
import contextlib
import io
import unittest

import controlled_probe as c
import probe
from test_probe import FakeReader, synthetic_case
from test_metadata_review import FakeAPI, PATH


class FakeProbeAPI(FakeAPI):
    def __init__(self):
        super().__init__()
        self.events = []
        self.signature_bad = False
        self.scan_error = False
        self.match = True
        self.short_page = False
        self.real_synthetic_block = None
        self.real_synthetic_page = None

    def open_target(self, path):
        self.events.append(("open_target", path))
        return self.open_metadata(path)

    def verify_signature(self, thumbprint):
        self.events.append(("signature", thumbprint))
        if self.signature_bad:
            raise PermissionError("synthetic")

    def first_page(self, handle):
        self.events.append(("page", handle))
        if self.real_synthetic_page is not None:
            return self.real_synthetic_page
        return b"P" * (1 if self.short_page else 4096)

    def matches_page(self, handle, page):
        self.events.append(("scan", handle))
        if self.scan_error:
            raise TimeoutError("synthetic")
        if self.real_synthetic_block is not None:
            return probe._scan_for_page_match(FakeReader(self.real_synthetic_block, ""), page)
        return self.match


class ControlledTests(unittest.TestCase):
    def test_match_uses_same_held_handles_and_prints_nothing(self):
        fake = FakeProbeAPI()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = c.probe_once(12, PATH, "A" * 40, api=fake)
        self.assertEqual(result, "MATCH")
        self.assertIn(("page", PATH), fake.events)
        self.assertIn(("scan", "process"), fake.events)
        self.assertEqual(fake.closed, list(reversed(fake.handles)))
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_end_to_end_with_synthetic_memory_and_mac(self):
        block, page = synthetic_case()
        fake = FakeProbeAPI()
        fake.real_synthetic_block = block
        fake.real_synthetic_page = page
        self.assertEqual(c.probe_once(12, PATH, "A" * 40, api=fake), "MATCH")
        fake = FakeProbeAPI()
        fake.real_synthetic_block = block
        fake.real_synthetic_page = page[:100] + bytes([page[100] ^ 1]) + page[101:]
        self.assertEqual(c.probe_once(12, PATH, "A" * 40, api=fake), "NOT_VERIFIED")
    def test_unchanged_identity_is_required_after_match(self):
        fake = FakeProbeAPI()
        fake.file_change = PATH
        self.assertEqual(c.probe_once(12, PATH, "A" * 40, api=fake), "NOT_VERIFIED")
        self.assertIn(("scan", "process"), fake.events)
        self.assertEqual(fake.closed, list(reversed(fake.handles)))

    def test_preflight_change_or_reparse_never_scans(self):
        for field, value in (("window_change", True), ("process_change", True),
                             ("bad_reparse", PATH), ("wrong_image", True)):
            fake = FakeProbeAPI()
            setattr(fake, field, value)
            self.assertEqual(c.probe_once(12, PATH, "A" * 40, api=fake), "NOT_VERIFIED")
            self.assertFalse(any(event[0] in ("page", "scan") for event in fake.events))
            self.assertEqual(fake.closed, list(reversed(fake.handles)))

    def test_failure_paths_return_only_not_verified(self):
        for field in ("signature_bad", "scan_error", "short_page"):
            fake = FakeProbeAPI()
            setattr(fake, field, True)
            self.assertEqual(c.probe_once(12, PATH, "A" * 40, api=fake), "NOT_VERIFIED")
            self.assertEqual(fake.closed, list(reversed(fake.handles)))
        fake = FakeProbeAPI()
        fake.match = False
        self.assertEqual(c.probe_once(12, PATH, "A" * 40, api=fake), "NOT_VERIFIED")
        fake = FakeProbeAPI()
        self.assertEqual(c.probe_once(12, PATH, "invalid", api=fake), "NOT_VERIFIED")
        self.assertFalse(fake.handles)


if __name__ == "__main__":
    unittest.main()

