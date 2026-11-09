"""Metadata-only Windows candidate check; does not prove account identity.

No process-memory or file-content reads. No CLI or automatic execution.
"""
from __future__ import annotations

import ctypes
import ntpath
import os
import re
from ctypes import wintypes
from dataclasses import dataclass, field

_EXPECTED_EXE = "C:\\example\\Weixin\\Weixin.exe"
_REPARSE = 0x400
_DIRECTORY = 0x10


def _contact_components(path: str) -> tuple[str, ...]:
    if type(path) is not str or "/" in path or path.startswith("\\\\"):
        raise ValueError("A standard local path is required")
    drive, tail = ntpath.splitdrive(path)
    if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith("\\") or ":" in tail:
        raise ValueError("A standard drive path is required")
    parts = tail[1:].split("\\")
    if len(parts) < 4 or any(not part or part in (".", "..") or part[-1] in (" ", ".") for part in parts):
        raise ValueError("Invalid path components")
    if [v.casefold() for v in parts[-3:]] != ["db_storage", "contact", "contact.db"]:
        raise ValueError("The fixed contact.db layout is required")
    out = [drive + "\\"]
    for part in parts:
        out.append(ntpath.join(out[-1], part))
    return tuple(out)


@dataclass(frozen=True, repr=False)
class FileMetadata:
    final_path: str = field(repr=False)
    volume: int = field(repr=False)
    file_id: bytes = field(repr=False)
    attributes: int = field(repr=False)


@dataclass(frozen=True, repr=False)
class CandidateMetadata:
    """Values are process-local, deliberately not rendered in repr."""
    pid: int = field(repr=False)
    process_created: int = field(repr=False)
    file_volume: int = field(repr=False)
    file_id: bytes = field(repr=False)
    status: str = "candidate_only"


def _same_path(left: str, right: str) -> bool:
    prefix = chr(92) * 2 + "?" + chr(92)
    if left.startswith(prefix):
        left = left[len(prefix):]
    return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))


def collect_candidate(hwnd: int, contact_path: str, *, api=None) -> CandidateMetadata:
    """Collect candidate metadata only; never implies account authorization."""
    return _collect_candidate(hwnd, contact_path, api=api, operation=None)


def _collect_candidate(hwnd: int, contact_path: str, *, api=None, operation=None):
    """Internal operation runs only while all selected handles are held."""
    if type(hwnd) is not int or hwnd <= 0:
        raise ValueError("A confirmed window is required")
    paths = _contact_components(contact_path)
    adapter = api if api is not None else WindowsMetadataAPI()
    handles = []
    process_handle = None
    try:
        pid = adapter.window_pid(hwnd)
        if type(pid) is not int or pid <= 0:
            raise RuntimeError("Window identity unavailable")
        process_handle = adapter.open_process(pid)
        initial_process = adapter.process_metadata(process_handle)
        if initial_process[0] != pid or initial_process[1] <= 0 or not initial_process[2]:
            raise RuntimeError("Process identity unavailable")
        if not _same_path(initial_process[3], _EXPECTED_EXE):
            raise RuntimeError("Unexpected process image")
        first = []
        for index, path in enumerate(paths):
            if operation is not None and index == len(paths) - 1:
                handle = adapter.open_target(path)
            else:
                handle = adapter.open_metadata(path)
            handles.append(handle)
            info = adapter.file_metadata(handle)
            directory_expected = index < len(paths) - 1
            if (type(info) is not FileMetadata or info.attributes & _REPARSE
                    or bool(info.attributes & _DIRECTORY) != directory_expected
                    or not _same_path(info.final_path, path) or len(info.file_id) != 16):
                raise RuntimeError("File identity or path invalid")
            first.append(info)
        if adapter.window_pid(hwnd) != pid or adapter.process_metadata(process_handle) != initial_process:
            raise RuntimeError("Window or process changed")
        result = operation(adapter, process_handle, handles[-1]) if operation else None
        for handle, before in zip(handles, first):
            if adapter.file_metadata(handle) != before:
                raise RuntimeError("File identity changed")
        if adapter.window_pid(hwnd) != pid or adapter.process_metadata(process_handle) != initial_process:
            raise RuntimeError("Window or process changed")
        if operation is not None:
            return result
        target = first[-1]
        return CandidateMetadata(pid, initial_process[1], target.volume, target.file_id)
    finally:
        for handle in reversed(handles):
            adapter.close(handle)
        if process_handle is not None:
            adapter.close(process_handle)


class _FileAttributeTag(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]


class _FileId(ctypes.Structure):
    _fields_ = [("volume", ctypes.c_ulonglong), ("file_id", ctypes.c_ubyte * 16)]


class WindowsMetadataAPI:
    """Open metadata handles only; instantiate only on explicit invocation."""
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Windows required")
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        k, u = self.k, self.u
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.GetProcessId.argtypes = [wintypes.HANDLE]
        k.GetProcessId.restype = wintypes.DWORD
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k.GetProcessTimes.restype = wintypes.BOOL
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetExitCodeProcess.restype = wintypes.BOOL
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        k.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.GetFileInformationByHandleEx.restype = wintypes.BOOL
        k.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        k.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL

    def window_pid(self, hwnd):
        pid = wintypes.DWORD()
        if not self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            raise RuntimeError("Window metadata unavailable")
        return pid.value

    def open_process(self, pid):
        handle = self.k.OpenProcess(0x1000, False, pid)
        if not handle:
            raise RuntimeError("Process metadata unavailable")
        return handle

    def process_metadata(self, handle):
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not self.k.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            raise RuntimeError("Process time unavailable")
        code = wintypes.DWORD()
        if not self.k.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise RuntimeError("Process state unavailable")
        size = wintypes.DWORD(32768)
        path = ctypes.create_unicode_buffer(size.value)
        if not self.k.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            raise RuntimeError("Process path unavailable")
        stamp = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return self.k.GetProcessId(handle), stamp, code.value == 259, path.value

    def open_metadata(self, path):
        handle = self.k.CreateFileW(path, 0x80, 0x7, None, 3, 0x02200000, None)
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise RuntimeError("File metadata unavailable")
        return handle

    def file_metadata(self, handle):
        tag, identity = _FileAttributeTag(), _FileId()
        if not self.k.GetFileInformationByHandleEx(handle, 9, ctypes.byref(tag), ctypes.sizeof(tag)):
            raise RuntimeError("File attributes unavailable")
        if not self.k.GetFileInformationByHandleEx(handle, 18, ctypes.byref(identity), ctypes.sizeof(identity)):
            raise RuntimeError("File id unavailable")
        path = ctypes.create_unicode_buffer(32768)
        size = self.k.GetFinalPathNameByHandleW(handle, path, len(path), 0)
        if not size or size >= len(path):
            raise RuntimeError("Final path unavailable")
        return FileMetadata(path.value, identity.volume, bytes(identity.file_id), tag.attributes)

    def close(self, handle):
        self.k.CloseHandle(handle)


