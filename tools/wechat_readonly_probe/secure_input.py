"""Two stdin inputs: no-echo Windows console or caller-controlled pipe."""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes


class WindowsInput:
    def __init__(self, stream, *, kernel=None, handle=None):
        if os.name != "nt":
            raise RuntimeError("Windows required")
        if handle is None:
            import msvcrt
            handle = msvcrt.get_osfhandle(stream.fileno())
        self.handle = handle
        self.k = kernel if kernel is not None else ctypes.WinDLL("kernel32", use_last_error=True)
        self.k.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.k.GetConsoleMode.restype = wintypes.BOOL
        self.k.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.k.SetConsoleMode.restype = wintypes.BOOL
        self.k.GetFileType.argtypes = [wintypes.HANDLE]
        self.k.GetFileType.restype = wintypes.DWORD

    def begin(self):
        mode = wintypes.DWORD()
        if self.k.GetConsoleMode(self.handle, ctypes.byref(mode)):
            masked = (mode.value | 0x2) & ~0x4
            if not self.k.SetConsoleMode(self.handle, masked):
                raise RuntimeError("Cannot disable console echo")
            return ("console", mode.value)
        if self.k.GetFileType(self.handle) == 3:
            return ("pipe", None)
        raise RuntimeError("Console or pipe input required")

    def restore(self, ticket):
        if ticket[0] == "console" and not self.k.SetConsoleMode(self.handle, ticket[1]):
            raise RuntimeError("Cannot restore console mode")


def read_two(*, stream=None, adapter=None):
    """Read two values internally; never print or persist them."""
    source = sys.stdin if stream is None else stream
    control = WindowsInput(source) if adapter is None else adapter
    ticket = control.begin()
    try:
        values = []
        for _ in range(2):
            value = source.readline(203)
            if type(value) is not str:
                raise ValueError("Invalid input")
            value = value.rstrip("\r\n")
            if (not value or len(value) > 200 or value != value.strip()
                    or any(ord(char) < 32 for char in value)):
                raise ValueError("Invalid input")
            values.append(value)
        return tuple(values)
    finally:
        control.restore(ticket)

