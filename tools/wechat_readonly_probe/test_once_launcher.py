"""Launcher tests with fake config/paths/signature; never run real discovery."""
import importlib.util
import io
import ntpath
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import mock_open, patch

FILE = Path(__file__).resolve().parents[2] / ".tmp" / "wechat_probe_once_20261101.py"
spec = importlib.util.spec_from_file_location("synthetic_once_launcher", FILE)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class Entry:
    def __init__(self, name, *, is_file=False, is_dir=False, path="synthetic.ini"):
        self.name, self.path = name, path
        self._file, self._dir = is_file, is_dir

    def stat(self, follow_symlinks=False):
        return SimpleNamespace(st_size=20, st_file_attributes=0)

    def is_file(self, follow_symlinks=False):
        return self._file

    def is_dir(self, follow_symlinks=False):
        return self._dir


class Scan:
    def __init__(self, entries):
        self.entries = entries

    def __enter__(self):
        return iter(self.entries)

    def __exit__(self, *_):
        return None


class OnceTests(unittest.TestCase):
    def test_only_explicit_window_and_fixed_status(self):
        calls = []
        def runner(hwnd, path, thumbprint):
            calls.append((hwnd, path, thumbprint))
            return "MATCH"
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = launcher.decide(["0x12"], discover=lambda: "synthetic",
                                     signer=lambda: "A" * 40, runner=runner)
        self.assertEqual(status, "MATCH")
        self.assertEqual(calls, [(18, "synthetic", "A" * 40)])
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_bad_input_no_selection_and_exception_fail_closed(self):
        def never(*args):
            self.fail("unexpected call")
        for args in ([], ["0"], ["-1"], ["bad"], ["1", "2"]):
            self.assertEqual(launcher.decide(args, discover=never, signer=never, runner=never), "NOT_VERIFIED")
        self.assertEqual(launcher.decide(["1"], discover=lambda: None, signer=lambda: None, runner=never), "NOT_VERIFIED")
        self.assertEqual(launcher.decide(["1"], discover=lambda: 1 / 0, signer=never, runner=never), "NOT_VERIFIED")

    def test_unique_candidate_from_fake_config(self):
        root = r"E:\synthetic_root"
        target = ntpath.join(root, "xwechat_files", "account", "db_storage", "contact", "contact.db")
        def scandir(path):
            if path.endswith("config"):
                return Scan([Entry("data.ini", is_file=True)])
            if ntpath.normcase(path) == ntpath.normcase(ntpath.join(root, "xwechat_files")):
                return Scan([Entry("account", is_dir=True)])
            raise OSError("unexpected fake path")
        def isdir(path):
            return ntpath.normcase(path) == ntpath.normcase(ntpath.join(root, "xwechat_files"))
        def isfile(path):
            return ntpath.normcase(path) == ntpath.normcase(target)
        with patch.dict(launcher.os.environ, {"APPDATA": r"C:\synthetic"}, clear=False),              patch.object(launcher.os, "scandir", side_effect=scandir),              patch.object(launcher.os.path, "isdir", side_effect=isdir),              patch.object(launcher.os.path, "isfile", side_effect=isfile),              patch("builtins.open", mock_open(read_data=b"save=E:\\synthetic_root\n")):
            self.assertEqual(launcher.discover_contact(), target)

    def test_multiple_fake_candidates_fail(self):
        def scandir(path):
            if path.endswith("config"):
                return Scan([Entry("data.ini", is_file=True)])
            return Scan([Entry("one", is_dir=True), Entry("two", is_dir=True)])
        with patch.dict(launcher.os.environ, {"APPDATA": r"C:\synthetic"}, clear=False),              patch.object(launcher.os, "scandir", side_effect=scandir),              patch.object(launcher.os.path, "isdir", return_value=True),              patch.object(launcher.os.path, "isfile", return_value=True),              patch("builtins.open", mock_open(read_data=b"E:\\synthetic_root\n")):
            self.assertIsNone(launcher.discover_contact())

    def test_signer_requires_valid_and_exact_cn_command(self):
        fake = SimpleNamespace(returncode=0, stdout=b"A" * 40)
        with patch.dict(launcher.os.environ, {"SystemRoot": r"C:\Windows"}, clear=False),              patch.object(launcher.subprocess, "run", return_value=fake) as call:
            self.assertEqual(launcher.trusted_signer(), "A" * 40)
        args, kwargs = call.call_args
        command = args[0][-1]
        self.assertIn("Get-AuthenticodeSignature", command)
        self.assertIn("Microsoft.PowerShell.Security.psd1", command)
        self.assertIn("Tencent Technology (Shenzhen) Company Limited", command)
        self.assertIn("'Valid'", command)
        self.assertTrue(kwargs["capture_output"])


if __name__ == "__main__":
    unittest.main()


