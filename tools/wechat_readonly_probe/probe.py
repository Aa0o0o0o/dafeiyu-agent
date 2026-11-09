"""Opt-in key-presence probe, not a database reader.

Adapted concepts from fanyuantaier/wechatauto-replica db.py at
bc2e6f2461330eb1a53996d2f2afc18416376b52, Apache-2.0.
This implementation is substantially changed: explicit identity and page,
bounded reads, no saved or returned key, no account or process discovery.
Never run against a real process without the user's explicit permission.
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
import re
import struct
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

_NAME = b"com.Tencent.WCDB.Config.Cipher"
_MASK = bytes.fromhex("d2c7442458020000004889442450488b450048844c2448488944254048584c24")
_LITERAL = re.compile(rb"[xX]'([0-9a-fA-F]{64}(?:[0-9a-fA-F]{32})?)'")
_MAX_CHUNK = 1024 * 1024
_MAX_HITS = 256
_MAX_BYTES = 2 * 1024 * 1024 * 1024
_MAX_SECONDS = 90
_READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}


def _mac_matches(candidate: bytes, page: bytes) -> bool:
    key = candidate[:32]
    salt = page[:16] if len(candidate) == 32 else candidate[32:]
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=32)
    calculated = hmac.digest(mac_key, page[16:4032] + struct.pack("<I", 1), "sha512")
    return hmac.compare_digest(calculated, page[4032:])


def _hits(source, needle: bytes):
    """Yield bounded pattern hits over readable regions, including boundaries."""
    hits = 0
    for start, size in source.regions():
        overlap = b""
        for offset in range(0, size, _MAX_CHUNK):
            address = start + offset
            data = source.read(address, min(_MAX_CHUNK, size - offset))
            if data is None:
                overlap = b""
                continue
            haystack = overlap + data
            pos = haystack.find(needle)
            while pos >= 0:
                absolute = address - len(overlap) + pos
                yield absolute
                hits += 1
                if hits >= _MAX_HITS:
                    return
                pos = haystack.find(needle, pos + 1)
            overlap = haystack[-(len(needle) - 1):] if len(needle) > 1 else b""


def _check_candidate(source, pair_at: int, marker: int, page: bytes) -> bytes | None:
    if pair_at < 16:
        return False
    node = source.read(pair_at - 16, 0x50)
    if node is None or len(node) != 0x50:
        return False
    pointer, length = struct.unpack_from("<QQ", node, 0x10)
    if pointer != marker or length != len(_NAME):
        return False
    config = struct.unpack_from("<Q", node, 0x28)[0]
    if not 0x10000 <= config < 0x800000000000:
        return False
    obj = source.read(config + 0x88, 0x28)
    if obj is None or len(obj) != 0x28:
        return False
    data_ptr, data_len = struct.unpack_from("<QQ", obj, 8)
    if not (0x10000 <= data_ptr < 0x800000000000 and 0 < data_len <= 1024):
        return False
    encoded = source.read(data_ptr, data_len)
    if encoded is None or len(encoded) != data_len:
        return False
    decoded = bytes(value ^ _MASK[i % len(_MASK)] for i, value in enumerate(encoded))
    for match in _LITERAL.finditer(decoded):
        candidate = bytes.fromhex(match.group(1).decode("ascii"))
        if _mac_matches(candidate, page):
            return candidate
        if len(candidate) == 48 and _mac_matches(candidate[:32], page):
            return candidate[:32]
    return False


def _find_page_key(source, page: bytes) -> bytes | None:
    if type(page) is not bytes or len(page) != 4096:
        raise ValueError("A single explicit 4096-byte database page is required")
    for marker in _hits(source, _NAME):
        pair = struct.pack("<QQ", marker, len(_NAME))
        for pair_at in _hits(source, pair):
            candidate = _check_candidate(source, pair_at, marker, page)
            if candidate:
                return candidate
    return None


def _scan_for_page_match(source, page: bytes) -> bool:
    return bool(_find_page_key(source, page))


class _Mbi(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("alignment1", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("alignment2", wintypes.DWORD),
    ]


class _WindowsReader:
    """PROCESS_QUERY_INFORMATION | PROCESS_VM_READ only; no write operations."""
    def __init__(self, pid: int):
        if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise RuntimeError("64-bit Windows is required")
        self._remaining_bytes = _MAX_BYTES
        self._deadline = time.monotonic() + _MAX_SECONDS
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.k32
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(_Mbi), ctypes.c_size_t]
        k.VirtualQueryEx.restype = ctypes.c_size_t
        k.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        k.ReadProcessMemory.restype = wintypes.BOOL
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.handle = k.OpenProcess(0x0400 | 0x0010, False, pid)
        if not self.handle:
            raise PermissionError("Could not open explicitly selected process")

    def close(self):
        if self.handle:
            self.k32.CloseHandle(self.handle)
            self.handle = None

    def image_path(self) -> str:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not self.k32.QueryFullProcessImageNameW(self.handle, 0, buf, ctypes.byref(size)):
            raise RuntimeError("Could not verify process image")
        return buf.value

    def regions(self):
        address = 0
        while address < 0x800000000000:
            if time.monotonic() > self._deadline:
                raise TimeoutError("Bounded probe timed out")
            mbi = _Mbi()
            if not self.k32.VirtualQueryEx(self.handle, address, ctypes.byref(mbi), ctypes.sizeof(mbi)):
                return
            start = mbi.BaseAddress or 0
            size = mbi.RegionSize
            if size <= 0 or start + size <= address:
                return
            if mbi.State == 0x1000 and not mbi.Protect & 0x100 and (mbi.Protect & 0xFF) in _READABLE:
                yield start, size
            address = start + size

    def read(self, address: int, size: int):
        if size <= 0 or size > _MAX_CHUNK:
            return None
        if time.monotonic() > self._deadline or size > self._remaining_bytes:
            raise TimeoutError("Bounded probe limit reached")
        self._remaining_bytes -= size
        buf = ctypes.create_string_buffer(size)
        nread = ctypes.c_size_t()
        ok = self.k32.ReadProcessMemory(self.handle, address, buf, size, ctypes.byref(nread))
        if not ok or nread.value != size:
            return None
        return buf.raw


def _verify_signature(path: str, trusted_thumbprint: str) -> None:
    if not re.fullmatch(r"[0-9A-Fa-f]{40}", trusted_thumbprint):
        raise ValueError("A separately verified certificate thumbprint is required")
    root = os.environ.get("SystemRoot")
    if not root:
        raise RuntimeError("SystemRoot is unavailable")
    executable = str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    env = os.environ.copy()
    env["DFY_SIGN_FILE"] = path
    command = (r"Import-Module -Name (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop;"
               "$s=Get-AuthenticodeSignature -LiteralPath $env:DFY_SIGN_FILE;"
               "if($s.Status -ne 'Valid' -or $null -eq $s.SignerCertificate){exit 1};"
               "[Console]::Write($s.SignerCertificate.Thumbprint)")
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", command],
                            env=env, capture_output=True, timeout=20, check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0 or result.stdout.strip().upper() != trusted_thumbprint.upper().encode("ascii"):
        raise PermissionError("Executable signature did not match the trusted signer")


def probe_selected_process(*, pid: int, expected_path: str, trusted_signer_thumbprint: str,
                           first_page: bytes) -> bool:
    """Manually invoked probe returning only a match flag; no key is returned."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("An explicit process id is required")
    if type(expected_path) is not str or not os.path.isabs(expected_path):
        raise ValueError("An explicit absolute executable path is required")
    if os.path.basename(expected_path).lower() != "weixin.exe":
        raise ValueError("Only the explicitly selected Weixin.exe is accepted")
    if type(first_page) is not bytes or len(first_page) != 4096:
        raise ValueError("A single explicit 4096-byte database page is required")
    if type(trusted_signer_thumbprint) is not str or not re.fullmatch(r"[0-9A-Fa-f]{40}", trusted_signer_thumbprint):
        raise ValueError("A separately verified certificate thumbprint is required")
    reader = _WindowsReader(pid)
    try:
        actual_path = reader.image_path()
        if os.path.normcase(os.path.realpath(actual_path)) != os.path.normcase(os.path.realpath(expected_path)):
            raise PermissionError("Selected process image path mismatch")
        _verify_signature(actual_path, trusted_signer_thumbprint)
        if os.path.normcase(os.path.realpath(reader.image_path())) != os.path.normcase(os.path.realpath(expected_path)):
            raise PermissionError("Selected process image path changed")
        return _scan_for_page_match(reader, first_page)
    finally:
        reader.close()




