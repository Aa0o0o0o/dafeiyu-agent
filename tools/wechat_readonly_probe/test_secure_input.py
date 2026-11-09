"""Only fake values are used; never opens a real interactive console."""
import contextlib
import ctypes
import io
import os
import unittest
from ctypes import wintypes

import secure_input


class FakeFunction:
    def __init__(self, function):
        self.function = function
        self.argtypes = None
        self.restype = None
    def __call__(self, *args):
        return self.function(*args)


class FakeKernel:
    def __init__(self, *, console=True, file_type=3, mode=7, set_ok=True):
        self.modes = []
        def get_mode(handle, pointer):
            if console:
                ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))[0] = mode
                return 1
            return 0
        def set_mode(handle, new_mode):
            self.modes.append(new_mode)
            return int(set_ok)
        self.GetConsoleMode = FakeFunction(get_mode)
        self.SetConsoleMode = FakeFunction(set_mode)
        self.GetFileType = FakeFunction(lambda handle: file_type)


class InputTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows API shape")
    def test_console_echo_disabled_and_restored(self):
        kernel = FakeKernel(mode=7)
        stream = io.StringIO("fake-owner\nfake-target\n")
        adapter = secure_input.WindowsInput(stream, kernel=kernel, handle=42)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = secure_input.read_two(stream=stream, adapter=adapter)
        self.assertEqual(result, ("fake-owner", "fake-target"))
        self.assertEqual(kernel.modes, [3, 7])
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    @unittest.skipUnless(os.name == "nt", "Windows API shape")
    def test_console_restored_after_invalid_input(self):
        kernel = FakeKernel(mode=7)
        stream = io.StringIO("fake-owner\n" + "x" * 201 + "\n")
        adapter = secure_input.WindowsInput(stream, kernel=kernel, handle=42)
        with self.assertRaises(ValueError):
            secure_input.read_two(stream=stream, adapter=adapter)
        self.assertEqual(kernel.modes, [3, 7])

    @unittest.skipUnless(os.name == "nt", "Windows API shape")
    def test_set_mode_failure_and_regular_file_rejected(self):
        kernel = FakeKernel(mode=7, set_ok=False)
        adapter = secure_input.WindowsInput(io.StringIO(""), kernel=kernel, handle=42)
        with self.assertRaises(RuntimeError):
            secure_input.read_two(stream=io.StringIO(""), adapter=adapter)
        kernel = FakeKernel(console=False, file_type=1)
        adapter = secure_input.WindowsInput(io.StringIO(""), kernel=kernel, handle=42)
        with self.assertRaises(RuntimeError):
            secure_input.read_two(stream=io.StringIO(""), adapter=adapter)

    @unittest.skipUnless(os.name == "nt", "Windows pipe test")
    def test_actual_anonymous_pipe_with_synthetic_values(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"fake-owner\nfake-target\n")
        finally:
            os.close(write_fd)
        with os.fdopen(read_fd, "r", encoding="utf-8") as stream:
            self.assertEqual(secure_input.read_two(stream=stream), ("fake-owner", "fake-target"))


if __name__ == "__main__":
    unittest.main()
