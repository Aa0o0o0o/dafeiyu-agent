"""Fake-only tests of metadata orchestration; no Windows objects are opened."""
import hashlib
import unittest

import metadata_review as m

PATH = r"E:\synthetic\account\db_storage\contact\contact.db"


class FakeAPI:
    def __init__(self):
        self.handles = []
        self.closed = []
        self.calls = {}
        self.bad_reparse = None
        self.file_change = None
        self.window_change = False
        self.process_change = False
        self.wrong_image = False
        self.fail_open_at = None
        self.window_calls = 0
        self.proc_calls = 0

    def window_pid(self, hwnd):
        self.window_calls += 1
        return 88 if self.window_change and self.window_calls > 1 else 77

    def open_process(self, pid):
        self.handles.append("process")
        return "process"

    def process_metadata(self, handle):
        self.proc_calls += 1
        created = 1001 if self.process_change and self.proc_calls > 1 else 1000
        return 77, created, True, r"D:\wrong\Weixin.exe" if self.wrong_image else m._EXPECTED_EXE

    def open_metadata(self, path):
        if path == self.fail_open_at:
            raise RuntimeError("synthetic failure")
        self.handles.append(path)
        return path

    def file_metadata(self, handle):
        self.calls[handle] = self.calls.get(handle, 0) + 1
        attrs = 0 if handle.casefold().endswith("contact.db") else m._DIRECTORY
        if self.bad_reparse == handle:
            attrs |= m._REPARSE
        ident = hashlib.sha256(handle.encode()).digest()[:16]
        if self.file_change == handle and self.calls[handle] > 1:
            ident = b"Z" * 16
        return m.FileMetadata(handle, 5, ident, attrs)

    def close(self, handle):
        self.closed.append(handle)


class MetadataTests(unittest.TestCase):
    def test_success_is_candidate_only_and_closes_everything(self):
        fake = FakeAPI()
        result = m.collect_candidate(12, PATH, api=fake)
        self.assertEqual(result.status, "candidate_only")
        self.assertEqual(result.pid, 77)
        self.assertEqual(len(result.file_id), 16)
        self.assertEqual(fake.closed, list(reversed(fake.handles)))
        self.assertNotIn(PATH, repr(result))

    def test_each_reparse_is_rejected_and_closed(self):
        for path in m._contact_components(PATH):
            fake = FakeAPI()
            fake.bad_reparse = path
            with self.assertRaises(RuntimeError):
                m.collect_candidate(12, PATH, api=fake)
            self.assertEqual(fake.closed, list(reversed(fake.handles)))

    def test_window_process_and_file_changes_abort(self):
        for field, value in (("window_change", True), ("process_change", True),
                             ("wrong_image", True), ("file_change", PATH)):
            fake = FakeAPI()
            setattr(fake, field, value)
            with self.assertRaises(RuntimeError):
                m.collect_candidate(12, PATH, api=fake)
            self.assertEqual(fake.closed, list(reversed(fake.handles)))

    def test_open_failure_closes_prior_handles(self):
        fake = FakeAPI()
        fake.fail_open_at = m._contact_components(PATH)[2]
        with self.assertRaises(RuntimeError):
            m.collect_candidate(12, PATH, api=fake)
        self.assertEqual(fake.closed, list(reversed(fake.handles)))

    def test_invalid_paths_rejected_before_handles(self):
        fake = FakeAPI()
        for path in ("E:" + chr(92), r"\\server\account\db_storage\contact\contact.db",
                     r"E:\account\db_storage\contact\contact.db:other",
                     r"E:\account\..\db_storage\contact\contact.db",
                     r"E:\account\db_storage\other.db"):
            with self.assertRaises(ValueError):
                m.collect_candidate(12, path, api=fake)
        self.assertEqual(fake.handles, [])


if __name__ == "__main__":
    unittest.main()

