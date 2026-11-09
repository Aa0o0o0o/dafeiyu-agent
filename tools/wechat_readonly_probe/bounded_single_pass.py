"""Bounded one-pass candidate association; no standalone process access."""
from __future__ import annotations

import bisect
import struct
import time
from collections import defaultdict

if __package__:
    from . import probe
else:
    import probe

_MAX_REGIONS = 65536
_MAX_MARKERS = 256
_MAX_REFERENCES = 4096


class _RegionReader:
    """Ensure all reads stay within enumerated readable regions."""
    def __init__(self, source, regions):
        self.source = source
        self.regions = regions
        self.starts = [start for start, _ in regions]
        self.incomplete = False

    def read(self, address, size):
        index = bisect.bisect_right(self.starts, address) - 1
        if index < 0 or size <= 0:
            self.incomplete = True
            return None
        start, extent = self.regions[index]
        if address < start or address + size > start + extent:
            self.incomplete = True
            return None
        data = self.source.read(address, size)
        if data is None or len(data) != size:
            self.incomplete = True
            return None
        return data


def _positions(data, needle):
    pos = data.find(needle)
    while pos >= 0:
        yield pos
        pos = data.find(needle, pos + 1)


def scan(source, page: bytes, *, chunk_size: int = probe._MAX_CHUNK) -> str:
    """One bounded operation; caller owns the shared byte/time budget."""
    return _scan(source, page, chunk_size=chunk_size, consumer=None)


def _scan(source, page: bytes, *, chunk_size: int, consumer) -> str:
    """Internal candidate handoff; not exposed to the bot or public probe."""
    if type(page) is not bytes or len(page) != 4096 or type(chunk_size) is not int:
        return "SCAN_ERROR"
    if not 1 <= chunk_size <= probe._MAX_CHUNK:
        return "SCAN_ERROR"
    try:
        deadline = getattr(source, "_deadline", None)

        def check_deadline():
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("Bounded probe deadline")

        regions = []
        for start, size in source.regions():
            check_deadline()
            if (type(start) is not int or type(size) is not int or start < 0 or size <= 0
                    or (regions and start < regions[-1][0] + regions[-1][1])):
                return "SCAN_INCOMPLETE"
            if len(regions) >= _MAX_REGIONS:
                return "SCAN_INCOMPLETE"
            regions.append((start, size))
        reader = _RegionReader(source, regions)
        markers = set()
        pending = defaultdict(list)
        references = set()
        overflow = False
        seen_matching_reference = False
        length = struct.pack("<Q", len(probe._NAME))
        overlap_size = max(len(probe._NAME) - 1, 15)

        for start, size in regions:
            overlap = b""
            for offset in range(0, size, chunk_size):
                check_deadline()
                address = start + offset
                raw = reader.read(address, min(chunk_size, size - offset))
                if raw is None:
                    overlap = b""
                    continue
                window = overlap + raw
                window_start = address - len(overlap)

                for pos in _positions(window, probe._NAME):
                    check_deadline()
                    marker = window_start + pos
                    if marker in markers:
                        continue
                    if len(markers) >= _MAX_MARKERS:
                        overflow = True
                        continue
                    markers.add(marker)
                    for pair_at in pending.pop(marker, ()):
                        seen_matching_reference = True
                        candidate = probe._check_candidate(reader, pair_at, marker, page)
                        if candidate:
                            try:
                                if consumer is None:
                                    return "MATCH"
                                status = consumer(candidate)
                                allowed = {"SYNTHETIC_IDENTITIES_MATCHED", "NOT_VERIFIED",
                                           "SCHEMA_TABLE_NOT_FOUND", "UNLOCK_NOT_VERIFIED",
                                           "SOURCE_CHANGED", "SIGNATURE_NOT_VERIFIED",
                                           "WAL_SHM_NOT_VERIFIED", "IDENTITIES_PRESENT_UNBOUND",
                                           "IDENTITIES_NOT_VERIFIED"}
                                allowed.update(f"SCHEMA_COLS_{mask:X}" for mask in range(16))
                                if type(status) is not str or status not in allowed:
                                    return "SCAN_ERROR"
                                return status
                            finally:
                                candidate = None

                for pos in _positions(window, length):
                    check_deadline()
                    if pos < 8:
                        continue
                    pointer = struct.unpack_from("<Q", window, pos - 8)[0]
                    if not 0x10000 <= pointer < 0x800000000000:
                        continue
                    pair_at = window_start + pos - 8
                    if pair_at in references:
                        continue
                    if len(references) >= _MAX_REFERENCES:
                        overflow = True
                        continue
                    references.add(pair_at)
                    if pointer in markers:
                        seen_matching_reference = True
                        candidate = probe._check_candidate(reader, pair_at, pointer, page)
                        if candidate:
                            try:
                                if consumer is None:
                                    return "MATCH"
                                status = consumer(candidate)
                                allowed = {"SYNTHETIC_IDENTITIES_MATCHED", "NOT_VERIFIED",
                                           "SCHEMA_TABLE_NOT_FOUND", "UNLOCK_NOT_VERIFIED",
                                           "SOURCE_CHANGED", "SIGNATURE_NOT_VERIFIED",
                                           "WAL_SHM_NOT_VERIFIED", "IDENTITIES_PRESENT_UNBOUND",
                                           "IDENTITIES_NOT_VERIFIED"}
                                allowed.update(f"SCHEMA_COLS_{mask:X}" for mask in range(16))
                                if type(status) is not str or status not in allowed:
                                    return "SCAN_ERROR"
                                return status
                            finally:
                                candidate = None
                    else:
                        pending[pointer].append(pair_at)
                overlap = window[-overlap_size:]
        if overflow or reader.incomplete:
            return "SCAN_INCOMPLETE"
        if not markers:
            return "MARKER_NOT_VERIFIED"
        if not seen_matching_reference:
            return "CONFIG_REFERENCE_NOT_VERIFIED"
        return "CONFIG_CANDIDATE_NOT_VERIFIED"
    except TimeoutError:
        return "SCAN_LIMIT"
    except Exception:
        return "SCAN_ERROR"






