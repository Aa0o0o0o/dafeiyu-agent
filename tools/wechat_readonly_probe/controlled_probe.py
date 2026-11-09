"""One explicit, bounded contact-page probe; not an importer and no CLI."""
from __future__ import annotations

import ctypes
import re
import sys
import threading
import time
from ctypes import wintypes

if __package__:
    from . import metadata_review as metadata, probe
else:
    import metadata_review as metadata
    import probe


def probe_once(hwnd: int, contact_path: str, signer_thumbprint: str, *, api=None) -> str:
    """Return only MATCH or NOT_VERIFIED; never return handles, page or key."""
    if (type(signer_thumbprint) is not str
            or not re.fullmatch(r"[0-9A-Fa-f]{40}", signer_thumbprint)):
        return "NOT_VERIFIED"
    if sys.gettrace() or sys.getprofile() or threading.gettrace() or threading.getprofile():
        return "NOT_VERIFIED"
    try:
        adapter = api if api is not None else WindowsProbeAPI()

        def check_once(selected, process_handle, target_handle):
            selected.verify_signature(signer_thumbprint)
            page = selected.first_page(target_handle)
            if type(page) is not bytes or len(page) != 4096:
                return False
            return selected.matches_page(process_handle, page) is True

        matched = metadata._collect_candidate(hwnd, contact_path, api=adapter, operation=check_once)
        return "MATCH" if matched is True else "NOT_VERIFIED"
    except Exception:
        return "NOT_VERIFIED"


class WindowsProbeAPI(metadata.WindowsMetadataAPI):
    """Extra permissions exist only in this manually invoked probe adapter."""
    def __init__(self):
        super().__init__()
        self.k.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        self.k.ReadFile.restype = wintypes.BOOL
        self.k.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                            ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
        self.k.SetFilePointerEx.restype = wintypes.BOOL
        self.k.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                          ctypes.POINTER(probe._Mbi), ctypes.c_size_t]
        self.k.VirtualQueryEx.restype = ctypes.c_size_t
        self.k.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.c_size_t,
                                             ctypes.POINTER(ctypes.c_size_t)]
        self.k.ReadProcessMemory.restype = wintypes.BOOL

    def open_process(self, pid):
        handle = self.k.OpenProcess(0x0400 | 0x0010, False, pid)
        if not handle:
            raise RuntimeError("Process unavailable")
        return handle

    def open_target(self, path):
        handle = self.k.CreateFileW(path, 0x81, 0x7, None, 3, 0x02200000, None)
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise RuntimeError("File unavailable")
        return handle

    def verify_signature(self, thumbprint):
        probe._verify_signature(metadata._EXPECTED_EXE, thumbprint)

    def first_page(self, handle):
        if not self.k.SetFilePointerEx(handle, 0, None, 0):
            raise RuntimeError("Page position unavailable")
        buf = ctypes.create_string_buffer(4096)
        size = wintypes.DWORD()
        if not self.k.ReadFile(handle, buf, 4096, ctypes.byref(size), None) or size.value != 4096:
            raise RuntimeError("Page unavailable")
        return buf.raw

    def matches_page(self, handle, page):
        reader = probe._WindowsReader.__new__(probe._WindowsReader)
        reader.k32 = self.k
        reader.handle = handle
        reader._remaining_bytes = probe._MAX_BYTES
        reader._deadline = time.monotonic() + probe._MAX_SECONDS
        return probe._scan_for_page_match(reader, page)

