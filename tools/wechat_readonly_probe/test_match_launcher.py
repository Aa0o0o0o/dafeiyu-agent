"""Mock-only launcher tests: no real discovery, scanning or private inputs."""
import importlib.util
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

FILE = Path(__file__).resolve().parents[2] / ".tmp" / "wechat_match_once_20261101.py"
spec = importlib.util.spec_from_file_location("synthetic_match_launcher", FILE)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_match_status_without_echo(self):
        calls = []
        def runner(hwnd, path, thumb, owner, target):
            calls.append((hwnd, path, thumb, owner, target))
            return "IDENTITIES_PRESENT_UNBOUND"
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = launcher.decide(["12"], discover=lambda: "fake-db",
                                     signer=lambda: "A" * 40,
                                     read_ids=lambda: ("fake-owner", "fake-target"), runner=runner)
        self.assertEqual(status, "IDENTITIES_PRESENT_UNBOUND")
        self.assertEqual(calls, [(12, "fake-db", "A" * 40, "fake-owner", "fake-target")])
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_invalid_or_untrusted_input_rejected(self):
        def never(*args):
            self.fail("unexpected fake operation")
        for args in ([], ["bad"], ["0"], ["1", "2"]):
            self.assertEqual(launcher.decide(args, discover=never), "INPUT_INVALID")
        self.assertEqual(launcher.decide(["1"], discover=lambda: None, signer=never), "DISCOVERY_NOT_VERIFIED")
        self.assertEqual(launcher.decide(["1"], discover=lambda: "fake", signer=lambda: None,
                                         read_ids=never), "SIGNATURE_NOT_VERIFIED")
        self.assertEqual(launcher.decide(["1"], discover=lambda: "fake", signer=lambda: "A" * 40,
                                         read_ids=lambda: ("one",), runner=never), "INPUT_NOT_VERIFIED")

    def test_no_unapproved_output_or_exception_details(self):
        def fail(*args):
            raise ValueError("fake-private-data")
        common = dict(discover=lambda: "fake", signer=lambda: "A" * 40,
                      read_ids=lambda: ("fake-owner", "fake-target"))
        self.assertEqual(launcher.decide(["1"], **common, runner=fail), "UNLOCK_NOT_VERIFIED")
        self.assertEqual(launcher.decide(["1"], **common,
                                         runner=lambda *_: "fake-private-data"), "UNLOCK_NOT_VERIFIED")


if __name__ == "__main__":
    unittest.main()
