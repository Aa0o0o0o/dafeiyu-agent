"""Explicit local source session. No CLI, model tool, scheduling, or auto-run.

Opening a session can read a selected Windows process and database pages. The
caller must have separately authenticated the selected WeChat account and user
consent. Metadata checks do not prove account identity, complete history, an
atomic multi-database snapshot, or that WAL/SHM activity is write-free. Only a
trusted local callback receives connections, which close on callback return.
"""
from __future__ import annotations

import hmac
import ntpath
import re
import struct
import sys
import threading
import time
from collections import defaultdict
from contextlib import ExitStack
from importlib import metadata as package_metadata
from pathlib import Path

from . import bounded_single_pass as bounded
from . import controlled_probe, metadata_review as metadata, probe, schema_probe


class SourceSessionError(RuntimeError):
    """Fixed, non-sensitive source failure category."""


def _scan_multi(source, pages, consumer, chunk_size=probe._MAX_CHUNK):
    """Read regions once; bounded candidate dereferences may add reads.

    Keys are handed only to the internal consumer and never returned or logged.
    MATCH means every supplied page authenticated, not that memory was exhausted.
    """
    if (not pages or len(pages) > 33 or any(type(p) is not bytes or len(p) != 4096 for p in pages)
            or type(chunk_size) is not int or not 1 <= chunk_size <= probe._MAX_CHUNK):
        return 'SCAN_ERROR'
    keys = [None] * len(pages)
    try:
        deadline = getattr(source, '_deadline', None)
        def check_deadline():
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError
        regions = []
        for start, size in source.regions():
            check_deadline()
            if (type(start) is not int or type(size) is not int or start < 0 or size <= 0
                    or (regions and start < regions[-1][0] + regions[-1][1])
                    or len(regions) >= bounded._MAX_REGIONS):
                return 'SCAN_INCOMPLETE'
            regions.append((start, size))
        reader = bounded._RegionReader(source, regions)
        markers, references = set(), set()
        pending = defaultdict(list)
        length = struct.pack('<Q', len(probe._NAME))
        overlap_size = max(len(probe._NAME) - 1, 15)
        overflow = False

        def inspect(pair_at, marker):
            for i, page in enumerate(pages):
                if keys[i] is not None:
                    continue
                check_deadline()
                candidate = probe._check_candidate(reader, pair_at, marker, page)
                if candidate:
                    keys[i] = candidate
            return all(key is not None for key in keys)

        for start, size in regions:
            overlap = b''
            for offset in range(0, size, chunk_size):
                check_deadline()
                address = start + offset
                raw = reader.read(address, min(chunk_size, size - offset))
                if raw is None:
                    overlap = b''
                    continue
                window = overlap + raw
                origin = address - len(overlap)
                for pos in bounded._positions(window, probe._NAME):
                    marker = origin + pos
                    if marker in markers:
                        continue
                    if len(markers) >= bounded._MAX_MARKERS:
                        overflow = True
                        continue
                    markers.add(marker)
                    for pair_at in pending.pop(marker, ()):
                        if inspect(pair_at, marker):
                            if overflow or reader.incomplete:
                                return 'SCAN_INCOMPLETE'
                            return 'MATCH' if consumer(keys) is True else 'SCAN_ERROR'
                for pos in bounded._positions(window, length):
                    if pos < 8:
                        continue
                    pointer = struct.unpack_from('<Q', window, pos - 8)[0]
                    if not 0x10000 <= pointer < 0x800000000000:
                        continue
                    pair_at = origin + pos - 8
                    if pair_at in references:
                        continue
                    if len(references) >= bounded._MAX_REFERENCES:
                        overflow = True
                        continue
                    references.add(pair_at)
                    if pointer in markers:
                        if inspect(pair_at, pointer):
                            if overflow or reader.incomplete:
                                return 'SCAN_INCOMPLETE'
                            return 'MATCH' if consumer(keys) is True else 'SCAN_ERROR'
                    else:
                        pending[pointer].append(pair_at)
                overlap = window[-overlap_size:]
        return 'SCAN_INCOMPLETE' if overflow or reader.incomplete else 'KEYS_NOT_VERIFIED'
    except TimeoutError:
        return 'SCAN_LIMIT'
    except Exception:
        return 'SCAN_ERROR'
    finally:
        keys.clear()


def _message_components(contact_path, message_paths, *, shard_kind='message'):
    if shard_kind not in ('message', 'media'):
        raise SourceSessionError('SOURCE_LIST_INVALID')
    contact_parts = metadata._contact_components(contact_path)
    db_root = contact_parts[-3]
    if not isinstance(message_paths, (tuple, list)) or not 1 <= len(message_paths) <= 32:
        raise SourceSessionError('SOURCE_LIST_INVALID')
    result = []
    for path in message_paths:
        if type(path) is not str or '/' in path or path.startswith('\\\\') or ':' in ntpath.splitdrive(path)[1]:
            raise SourceSessionError('SOURCE_LIST_INVALID')
        directory, name = ntpath.split(path)
        expected = ntpath.join(db_root, 'message', name)
        if (ntpath.normcase(path) != ntpath.normcase(expected)
                or not metadata._same_path(directory, ntpath.join(db_root, 'message'))
                or not re.fullmatch(shard_kind + r'_[0-9]+\.db', name, re.IGNORECASE)):
            raise SourceSessionError('SOURCE_LIST_INVALID')
        result.append(contact_parts[:-2] + (directory, path))
    if len({ntpath.normcase(p[-1]) for p in result}) != len(result):
        raise SourceSessionError('SOURCE_LIST_INVALID')
    return result


class _Session:
    def __init__(self, contact, shards, verifier):
        self.contact = contact
        self.shards = shards
        self._verifier = verifier

    def revalidate(self):
        try:
            if self._verifier() is not True:
                raise SourceSessionError('SOURCE_CHANGED')
            return True
        except Exception:
            raise SourceSessionError('SOURCE_CHANGED') from None


def with_real_sources(hwnd, contact_path, message_paths, signer_thumbprint, *, operation,
                      shard_kind='message', api=None, reader_factory=None, connection_factory=None, sidecars_fn=None):
    """Use explicit selected sources while handles and read-only connections live.

    `operation(session)` is trusted local code. It must authenticate the account
    and call `session.revalidate() is True` under its destination publish fence.
    Inputs are explicit: no discovery and no actual access occurs on import.
    For tests only, API/reader/connection/sidecar dependencies may be injected.
    """
    if (type(hwnd) is not int or hwnd <= 0 or not callable(operation)
            or type(signer_thumbprint) is not str
            or not re.fullmatch(r'[0-9A-Fa-f]{40}', signer_thumbprint)):
        raise SourceSessionError('SOURCE_INPUT_INVALID')
    if sys.gettrace() or sys.getprofile() or threading.gettrace() or threading.getprofile():
        raise SourceSessionError('SOURCE_NOT_VERIFIED')
    components = _message_components(contact_path, message_paths, shard_kind=shard_kind)
    adapter = api if api is not None else controlled_probe.WindowsProbeAPI()
    sidecars = sidecars_fn or schema_probe._sidecars
    make_reader = reader_factory or schema_probe._reader
    outcome = []
    try:
        def held(selected, process_handle, contact_handle):
            initial_process = selected.process_metadata(process_handle)
            if initial_process[0] != selected.window_pid(hwnd) or not initial_process[2]:
                raise SourceSessionError('SOURCE_NOT_VERIFIED')
            selected.verify_signature(signer_thumbprint)
            with ExitStack() as stack:
                target_paths = [contact_path]
                target_handles = [contact_handle]
                for chain in components:
                    for i, path in enumerate(chain):
                        handle = selected.open_target(path) if i == len(chain) - 1 else selected.open_metadata(path)
                        stack.callback(selected.close, handle)
                        info = selected.file_metadata(handle)
                        expected_dir = i < len(chain) - 1
                        if (type(info) is not metadata.FileMetadata or info.attributes & metadata._REPARSE
                                or bool(info.attributes & metadata._DIRECTORY) != expected_dir
                                or not metadata._same_path(info.final_path, path) or len(info.file_id) != 16):
                            raise SourceSessionError('SOURCE_NOT_VERIFIED')
                    target_paths.append(chain[-1])
                    target_handles.append(handle)
                sidecar_states, sidecar_handles = [], []
                for path, handle in zip(target_paths, target_handles, strict=True):
                    state = sidecars(path)
                    if type(state) is not tuple or len(state) != 2 or any(type(x) is not bool for x in state):
                        raise SourceSessionError('SOURCE_NOT_VERIFIED')
                    if state[0] and not state[1]:
                        raise SourceSessionError('SOURCE_NOT_VERIFIED')
                    sidecar_states.append(state)
                    volume = selected.file_metadata(handle).volume
                    for present, suffix in zip(state, ('-wal', '-shm'), strict=True):
                        if present:
                            name = path + suffix
                            h = selected.open_metadata(name)
                            stack.callback(selected.close, h)
                            info = selected.file_metadata(h)
                            if not schema_probe._sidecar_identity_valid(info, name, volume):
                                raise SourceSessionError('SOURCE_NOT_VERIFIED')
                            sidecar_handles.append((name, h, info))
                pages = [selected.first_page(handle) for handle in target_handles]
                if any(type(p) is not bytes or len(p) != 4096 for p in pages):
                    raise SourceSessionError('SOURCE_NOT_VERIFIED')

                def verify():
                    if (selected.window_pid(hwnd) != initial_process[0]
                            or selected.process_metadata(process_handle) != initial_process):
                        return False
                    if not schema_probe._same_sidecars(selected, sidecar_handles):
                        return False
                    for path, handle, state, page in zip(target_paths, target_handles, sidecar_states, pages, strict=True):
                        if (not schema_probe._same_target(selected, handle, path)
                                or sidecars(path) != state
                                or not hmac.compare_digest(selected.first_page(handle), page)):
                            return False
                    selected.verify_signature(signer_thumbprint)
                    return True

                if not verify():
                    raise SourceSessionError('SOURCE_CHANGED')
                def consume(keys):
                    if connection_factory is None:
                        import sqlcipher3
                        if package_metadata.version('sqlcipher3') != '0.6.4':
                            return False
                        connect = sqlcipher3.connect
                    else:
                        connect = connection_factory
                    connections = []
                    for path, key in zip(target_paths, keys, strict=True):
                        conn = connect(Path(path).as_uri() + '?mode=ro', uri=True)
                        stack.callback(conn.close)
                        if conn.execute('PRAGMA cipher_log_level=NONE').fetchone() != ('NONE',):
                            return False
                        conn.set_trace_callback(None)
                        conn.set_key(b"x'" + key.hex().encode('ascii') + b"'")
                        status = conn.execute('PRAGMA cipher_status').fetchone()
                        if status not in ((1,), ('1',)):
                            return False
                        conn.execute('PRAGMA query_only=ON')
                        conn.execute('PRAGMA trusted_schema=OFF')
                        conn.execute('PRAGMA temp_store=MEMORY')
                        if conn.execute('PRAGMA query_only').fetchone() != (1,):
                            return False
                        conn.execute('SELECT count(*) FROM main.sqlite_schema').fetchone()
                        connections.append(conn)
                    session = _Session(connections[0], tuple((ntpath.basename(path).lower(), c) for path, c in zip(target_paths[1:], connections[1:], strict=True)), verify)
                    session.revalidate()
                    outcome.append(operation(session))
                    session.revalidate()
                    return True
                reader = make_reader(selected, process_handle)
                status = _scan_multi(reader, pages, consume)
                if status != 'MATCH':
                    raise SourceSessionError(status)
                return True
        result = metadata._collect_candidate(hwnd, contact_path, api=adapter, operation=held)
        if result is not True or len(outcome) != 1:
            raise SourceSessionError('SOURCE_NOT_VERIFIED')
        return outcome[0]
    except SourceSessionError:
        raise
    except Exception:
        raise SourceSessionError('SOURCE_NOT_VERIFIED') from None
