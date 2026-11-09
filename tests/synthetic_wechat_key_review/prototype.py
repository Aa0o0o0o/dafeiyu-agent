"""Synthetic-only cipher-page validation; no process, filesystem or CLI access.

Research reference: wechatauto-replica bc2e6f2461330eb1a53996d2f2afc18416376b52
wechatauto/db.py, Apache-2.0. This independently written demonstration changes
the interface, rejects broader inputs and exposes no process-reading facility.
"""
import hashlib
import hmac
import re

_LITERAL = re.compile(rb"[xX]'([0-9a-fA-F]{64}(?:[0-9a-fA-F]{32})?)'")
_PAGE = 4096
_MAX_BLOCK = 1048576


def _matches(candidate: bytes, page: bytes) -> bool:
    if len(candidate) not in (32, 48) or len(page) != _PAGE:
        return False
    key = candidate[:32]
    salt = page[:16] if len(candidate) == 32 else candidate[32:]
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=32)
    message = page[16:4032] + (1).to_bytes(4, "little")
    return hmac.compare_digest(hmac.digest(mac_key, message, "sha512"), page[4032:])


def has_valid_candidate(block: bytes, page: bytes) -> bool:
    """Test exact raw-key literal(s) against one synthetic SQLCipher4 page."""
    if type(block) is not bytes or type(page) is not bytes:
        raise TypeError("Only explicit immutable byte strings are accepted")
    if len(block) > _MAX_BLOCK or len(page) != _PAGE:
        raise ValueError("Input size outside synthetic test bounds")
    for match in _LITERAL.finditer(block):
        candidate = bytes.fromhex(match.group(1).decode("ascii"))
        if _matches(candidate, page):
            return True
    return False


class FakeMemory:
    """Data-only synthetic source: no callback, subprocess, file or OS handle."""
    __slots__ = ("pid", "path", "blocks")

    def __init__(self, pid: int, path: str, blocks: tuple[bytes, ...]):
        if type(pid) is not int or pid <= 0 or type(path) is not str or not path:
            raise ValueError("Invalid synthetic identity")
        if type(blocks) is not tuple or any(type(b) is not bytes for b in blocks):
            raise TypeError("Synthetic blocks must be an exact tuple of bytes")
        if len(blocks) > 16 or any(len(b) > _MAX_BLOCK for b in blocks):
            raise ValueError("Synthetic region limits exceeded")
        self.pid, self.path, self.blocks = pid, path, blocks


def simulate_scan(source: FakeMemory, *, pid: int, trusted_path: str, page: bytes) -> bool:
    """Simulate a fixed identity check against an in-memory fake source only."""
    if type(source) is not FakeMemory:
        raise TypeError("Only the built-in synthetic source is supported")
    if type(pid) is not int or pid <= 0 or type(trusted_path) is not str or not trusted_path:
        raise ValueError("An explicit synthetic identity is required")
    if type(source.pid) is not int or type(source.path) is not str:
        raise TypeError("Synthetic identity was mutated")
    if type(source.blocks) is not tuple or len(source.blocks) > 16:
        raise TypeError("Synthetic regions were mutated")
    if any(type(block) is not bytes or len(block) > _MAX_BLOCK for block in source.blocks):
        raise TypeError("Synthetic regions were mutated")
    if source.pid != pid or source.path != trusted_path:
        raise ValueError("Synthetic identity mismatch")
    if type(page) is not bytes or len(page) != _PAGE:
        raise ValueError("An explicit synthetic page is required")
    return any(has_valid_candidate(block, page) for block in source.blocks)

