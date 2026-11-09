"""Explicit single-operation schema check. No CLI and no bot integration.

Only sqlite_schema and pragma_table_info are queried; no contact row is read.
"""
from __future__ import annotations

import hmac
import os
import re
import sys
import threading
import time
from importlib import metadata as package_metadata
from pathlib import Path

if __package__:
    from . import bounded_single_pass, controlled_probe, metadata_review, probe
else:
    import bounded_single_pass
    import controlled_probe
    import metadata_review
    import probe

_COLUMNS = ("username", "alias", "remark", "nick_name")


def _same_target(adapter, target_handle, path):
    original = adapter.file_metadata(target_handle)
    if (original.attributes & metadata_review._REPARSE
            or original.attributes & metadata_review._DIRECTORY
            or not metadata_review._same_path(original.final_path, path)):
        return False
    other = adapter.open_metadata(path)
    try:
        current = adapter.file_metadata(other)
        return (current.volume == original.volume and current.file_id == original.file_id
                and current.attributes == original.attributes
                and metadata_review._same_path(current.final_path, path))
    finally:
        adapter.close(other)


def _sidecars(path):
    return os.path.exists(path + "-wal"), os.path.exists(path + "-shm")


def _sidecar_identity_valid(info, path, volume):
    return (type(info) is metadata_review.FileMetadata and info.volume == volume
            and not info.attributes & metadata_review._REPARSE
            and not info.attributes & metadata_review._DIRECTORY
            and len(info.file_id) == 16 and metadata_review._same_path(info.final_path, path))


def _same_sidecars(adapter, handles):
    for path, handle, original in handles:
        if adapter.file_metadata(handle) != original:
            return False
        other = adapter.open_metadata(path)
        try:
            if adapter.file_metadata(other) != original:
                return False
        finally:
            adapter.close(other)
    return True


def _reader(api, handle):
    reader = probe._WindowsReader.__new__(probe._WindowsReader)
    reader.k32 = api.k
    reader.handle = handle
    reader._remaining_bytes = probe._MAX_BYTES
    reader._deadline = time.monotonic() + probe._MAX_SECONDS
    return reader


def schema_once(hwnd, contact_path, signer_thumbprint, *, api=None, reader_factory=None):
    """Return only a fixed schema bitmask or failure status; no rows or key."""
    return _run_once(hwnd, contact_path, signer_thumbprint, api=api,
                     reader_factory=reader_factory, display_ids=None)


def match_once(hwnd, contact_path, signer_thumbprint, owner_display, contact_display,
               *, api=None, reader_factory=None):
    """Exact match in the candidate contact table; does not prove account ownership."""
    ids = (owner_display, contact_display)
    if any(type(value) is not str or not value or value != value.strip()
           or len(value) > 200 or any(ord(char) < 32 for char in value) for value in ids):
        return "IDENTITIES_NOT_VERIFIED"
    if owner_display == contact_display:
        return "IDENTITIES_NOT_VERIFIED"
    return _run_once(hwnd, contact_path, signer_thumbprint, api=api,
                     reader_factory=reader_factory, display_ids=ids)


def _run_once(hwnd, contact_path, signer_thumbprint, *, api=None, reader_factory=None, display_ids=None):
    if (type(signer_thumbprint) is not str
            or not re.fullmatch(r"[0-9A-Fa-f]{40}", signer_thumbprint)):
        return "SIGNATURE_NOT_VERIFIED"
    if sys.gettrace() or sys.getprofile() or threading.gettrace() or threading.getprofile():
        return "UNLOCK_NOT_VERIFIED"
    try:
        adapter = api if api is not None else controlled_probe.WindowsProbeAPI()
        make_reader = reader_factory or _reader

        def operation(selected, process_handle, target_handle):
            try:
                selected.verify_signature(signer_thumbprint)
            except Exception:
                return "SIGNATURE_NOT_VERIFIED"
            if not _same_target(selected, target_handle, contact_path):
                return "SOURCE_CHANGED"
            sidecars = _sidecars(contact_path)
            if sidecars[0] and not sidecars[1]:
                return "WAL_SHM_NOT_VERIFIED"
            page = selected.first_page(target_handle)
            if type(page) is not bytes or len(page) != 4096:
                return "UNLOCK_NOT_VERIFIED"

            def consume(candidate):
                connection = None
                sidecar_handles = []
                try:
                    if not _same_target(selected, target_handle, contact_path):
                        return "SOURCE_CHANGED"
                    if _sidecars(contact_path) != sidecars:
                        return "SOURCE_CHANGED"
                    volume = selected.file_metadata(target_handle).volume
                    for exists, suffix in zip(sidecars, ("-wal", "-shm")):
                        if not exists:
                            continue
                        side_path = contact_path + suffix
                        handle = selected.open_metadata(side_path)
                        sidecar_handles.append((side_path, handle, None))
                        info = selected.file_metadata(handle)
                        sidecar_handles[-1] = (side_path, handle, info)
                        if not _sidecar_identity_valid(info, side_path, volume):
                            return "SOURCE_CHANGED"
                    if not _same_sidecars(selected, sidecar_handles):
                        return "SOURCE_CHANGED"
                    import sqlcipher3
                    if package_metadata.version("sqlcipher3") != "0.6.4":
                        return "UNLOCK_NOT_VERIFIED"
                    connection = sqlcipher3.connect(Path(contact_path).as_uri() + "?mode=ro", uri=True)
                    if connection.execute("PRAGMA cipher_log_level=NONE").fetchone() != ("NONE",):
                        return "UNLOCK_NOT_VERIFIED"
                    connection.set_trace_callback(None)
                    failed = False
                    try:
                        connection.set_key(b"x'" + candidate.hex().encode("ascii") + b"'")
                    except Exception:
                        failed = True
                    if failed:
                        return "UNLOCK_NOT_VERIFIED"
                    cipher_status = connection.execute("PRAGMA cipher_status").fetchone()
                    if (cipher_status is None or len(cipher_status) != 1
                            or not ((type(cipher_status[0]) is int and cipher_status[0] == 1)
                                    or (type(cipher_status[0]) is str and cipher_status[0] == "1"))):
                        return "UNLOCK_NOT_VERIFIED"
                    connection.execute("PRAGMA query_only=ON")
                    connection.execute("PRAGMA temp_store=MEMORY")
                    connection.execute("PRAGMA trusted_schema=OFF")
                    if connection.execute("PRAGMA query_only").fetchone() != (1,):
                        return "UNLOCK_NOT_VERIFIED"
                    exists = connection.execute(
                        "SELECT 1 FROM main.sqlite_schema "
                        "WHERE type='table' AND name='contact' COLLATE BINARY LIMIT 1"
                    ).fetchone()
                    if not exists:
                        status = "SCHEMA_TABLE_NOT_FOUND" if display_ids is None else "IDENTITIES_NOT_VERIFIED"
                    elif display_ids is None:
                        rows = connection.execute(
                            "SELECT name FROM pragma_table_info('contact') "
                            "WHERE name IN ('username','alias','remark','nick_name')"
                        ).fetchall()
                        present = {row[0] for row in rows}
                        mask = sum(1 << index for index, name in enumerate(_COLUMNS) if name in present)
                        status = f"SCHEMA_COLS_{mask:X}"
                    else:
                        connection.execute("BEGIN")
                        query = ("SELECT username FROM main.contact WHERE "
                                 "alias COLLATE BINARY = ? OR username COLLATE BINARY = ? LIMIT 2")
                        try:
                            owner_rows = connection.execute(query, (display_ids[0], display_ids[0])).fetchall()
                            target_rows = connection.execute(query, (display_ids[1], display_ids[1])).fetchall()
                            valid = len(owner_rows) == 1 and len(target_rows) == 1
                            if valid:
                                owner_id, target_id = owner_rows[0][0], target_rows[0][0]
                                values = (owner_id, target_id)
                                valid = (all(type(value) is str and 0 < len(value) <= 200
                                             and value == value.strip()
                                             and not any(ord(char) < 32 for char in value)
                                             for value in values)
                                         and owner_id != target_id and not owner_id.endswith("@chatroom")
                                         and not target_id.endswith("@chatroom"))
                            status = "IDENTITIES_PRESENT_UNBOUND" if valid else "IDENTITIES_NOT_VERIFIED"
                        finally:
                            connection.rollback()
                    connection.close()
                    connection = None
                    if not _same_target(selected, target_handle, contact_path):
                        return "SOURCE_CHANGED"
                    if _sidecars(contact_path) != sidecars or not _same_sidecars(selected, sidecar_handles):
                        return "SOURCE_CHANGED"
                    after = selected.first_page(target_handle)
                    if type(after) is not bytes or not hmac.compare_digest(page, after):
                        return "SOURCE_CHANGED"
                    try:
                        selected.verify_signature(signer_thumbprint)
                    except Exception:
                        return "SIGNATURE_NOT_VERIFIED"
                    return status
                except Exception:
                    return "UNLOCK_NOT_VERIFIED"
                finally:
                    if connection is not None:
                        try:
                            connection.close()
                        except Exception:
                            pass
                    for _, handle, _ in reversed(sidecar_handles):
                        try:
                            selected.close(handle)
                        except Exception:
                            pass

            return bounded_single_pass._scan(make_reader(selected, process_handle), page,
                                             chunk_size=probe._MAX_CHUNK, consumer=consume)

        result = metadata_review._collect_candidate(hwnd, contact_path, api=adapter, operation=operation)
        allowed = {"SCHEMA_TABLE_NOT_FOUND", "UNLOCK_NOT_VERIFIED", "SOURCE_CHANGED",
                   "SIGNATURE_NOT_VERIFIED", "WAL_SHM_NOT_VERIFIED",
                   "IDENTITIES_PRESENT_UNBOUND", "IDENTITIES_NOT_VERIFIED"}
        allowed.update(f"SCHEMA_COLS_{mask:X}" for mask in range(16))
        return result if type(result) is str and result in allowed else "UNLOCK_NOT_VERIFIED"
    except BaseException:
        return "UNLOCK_NOT_VERIFIED"




