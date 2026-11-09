"""Mock-only checks: never calls discovery, Win32 or real schema reader."""
import importlib.util
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

FILE = Path(__file__).resolve().parents[2] / ".tmp" / "wechat_schema_once_20261101.py"
spec = importlib.util.spec_from_file_location("synthetic_schema_launcher", FILE)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_single_fixed_status_no_output(self):
        calls = []
        def runner(hwnd, path, thumb):
            calls.append((hwnd, path, thumb))
            return "SCHEMA_COLS_F"
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = launcher.decide(["12"], discover=lambda: "fake-db",
                                     signer=lambda: "A" * 40, runner=runner)
        self.assertEqual(status, "SCHEMA_COLS_F")
        self.assertEqual(calls, [(12, "fake-db", "A" * 40)])
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_invalid_and_unverified_fail_before_reader(self):
        def never(*args):
            self.fail("unexpected fake read")
        for args in ([], ["bad"], ["0"], ["1", "2"]):
            self.assertEqual(launcher.decide(args, discover=never, signer=never, runner=never), "INPUT_INVALID")
        self.assertEqual(launcher.decide(["1"], discover=lambda: None, signer=never, runner=never), "DISCOVERY_NOT_VERIFIED")
        self.assertEqual(launcher.decide(["1"], discover=lambda: "fake", signer=lambda: None, runner=never), "SIGNATURE_NOT_VERIFIED")

    def test_unapproved_status_and_exception_are_sanitized(self):
        self.assertEqual(launcher.decide(["1"], discover=lambda: "fake", signer=lambda: "A" * 40,
                                         runner=lambda *_: "fake-private-data"), "UNLOCK_NOT_VERIFIED")
        def fail(*args):
            raise ValueError("fake-private-data")
        self.assertEqual(launcher.decide(["1"], discover=lambda: "fake", signer=lambda: "A" * 40,
                                         runner=fail), "UNLOCK_NOT_VERIFIED")


if __name__ == "__main__":
    unittest.main()
