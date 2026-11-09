"""Synthetic-only comparison of bounded scanning strategies.

Cannot open a process or file. All memory regions are explicitly provided bytes.
"""
from __future__ import annotations

import struct
from collections import defaultdict

import probe

_MAX_MARKERS = 256
_MAX_REFS = 4096


class SyntheticMemory:
    def __init__(self, regions, budget=2 * 1024 * 1024 * 1024, chunk=1024):
        if type(regions) is not tuple or not regions or type(budget) is not int or type(chunk) is not int:
            raise ValueError("Invalid synthetic input")
        if budget <= 0 or chunk <= 0 or chunk > 1048576:
            raise ValueError("Invalid synthetic bounds")
        if any(type(base) is not int or type(data) is not bytes or len(data) > 4194304
               for base, data in regions):
            raise ValueError("Synthetic regions required")
        self.data = regions
        self.budget = budget
        self.chunk = chunk
        self.bytes_read = 0
        self.calls = 0

    def regions(self):
        for base, data in self.data:
            yield base, len(data)

    def read(self, address, size):
        if size <= 0 or self.bytes_read + size > self.budget:
            raise TimeoutError("Synthetic budget")
        self.bytes_read += size
        self.calls += 1
        for base, data in self.data:
            offset = address - base
            if 0 <= offset and offset + size <= len(data):
                return data[offset:offset + size]
        return None


def _chunks(memory):
    for start, size in memory.regions():
        overlap = b""
        for offset in range(0, size, memory.chunk):
            address = start + offset
            data = memory.read(address, min(memory.chunk, size - offset))
            if data is None:
                overlap = b""
                continue
            window = overlap + data
            yield address - len(overlap), window
            overlap = window[-max(len(probe._NAME) - 1, 15):]


def _positions(haystack, needle):
    index = haystack.find(needle)
    while index >= 0:
        yield index
        index = haystack.find(needle, index + 1)


def _match(memory, page, pair_at, pointer):
    key = probe._check_candidate(memory, pair_at, pointer, page)
    result = bool(key)
    key = None
    return result


def two_pass(memory, page):
    if type(memory) is not SyntheticMemory or type(page) is not bytes or len(page) != 4096:
        raise ValueError("Synthetic inputs required")
    markers = set()
    overflow = False
    try:
        for start, window in _chunks(memory):
            for pos in _positions(window, probe._NAME):
                marker = start + pos
                if marker not in markers:
                    if len(markers) >= _MAX_MARKERS:
                        overflow = True
                    else:
                        markers.add(marker)
        if not markers:
            return "INCOMPLETE" if overflow else "NOT_VERIFIED"
        length = struct.pack("<Q", len(probe._NAME))
        seen = set()
        for start, window in _chunks(memory):
            for pos in _positions(window, length):
                if pos < 8:
                    continue
                pointer = struct.unpack_from("<Q", window, pos - 8)[0]
                if pointer not in markers:
                    continue
                pair_at = start + pos - 8
                if pair_at in seen:
                    continue
                if len(seen) >= _MAX_REFS:
                    overflow = True
                    continue
                seen.add(pair_at)
                if _match(memory, page, pair_at, pointer):
                    return "MATCH"
    except TimeoutError:
        return "LIMIT"
    return "INCOMPLETE" if overflow else "NOT_VERIFIED"


def one_pass(memory, page):
    if type(memory) is not SyntheticMemory or type(page) is not bytes or len(page) != 4096:
        raise ValueError("Synthetic inputs required")
    markers = set()
    pending = defaultdict(list)
    seen = set()
    overflow = False
    length = struct.pack("<Q", len(probe._NAME))
    try:
        for start, window in _chunks(memory):
            for pos in _positions(window, probe._NAME):
                marker = start + pos
                if marker in markers:
                    continue
                if len(markers) >= _MAX_MARKERS:
                    overflow = True
                    continue
                markers.add(marker)
                for pair_at in pending.pop(marker, ()):
                    if _match(memory, page, pair_at, marker):
                        return "MATCH"
            for pos in _positions(window, length):
                if pos < 8:
                    continue
                pointer = struct.unpack_from("<Q", window, pos - 8)[0]
                if not 0x10000 <= pointer < 0x800000000000:
                    continue
                pair_at = start + pos - 8
                if pair_at in seen:
                    continue
                if len(seen) >= _MAX_REFS:
                    overflow = True
                    continue
                seen.add(pair_at)
                if pointer in markers:
                    if _match(memory, page, pair_at, pointer):
                        return "MATCH"
                else:
                    pending[pointer].append(pair_at)
    except TimeoutError:
        return "LIMIT"
    return "INCOMPLETE" if overflow else "NOT_VERIFIED"
