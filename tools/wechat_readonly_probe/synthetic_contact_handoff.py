"""Synthetic-only same-process handoff; cannot prove real account identity.

The test fixture creates the only supported encrypted database and fake process
memory. There is no CLI, bot registration, real process adapter, or persistence.
"""
from __future__ import annotations

import os
import stat
import sys
import threading
from pathlib import Path

import binding_model
import bounded_single_pass
import probe
import synthetic_scan_comparison


def _valid_id(value):
    return (type(value) is str and bool(value) and value == value.strip()
            and len(value) <= 200 and not any(ord(char) < 32 for char in value))


def _same_stat(left, right):
    return (left.st_dev, left.st_ino, left.st_size, left.st_mtime_ns) == (
        right.st_dev, right.st_ino, right.st_size, right.st_mtime_ns
    )


def synthetic_match(*, memory, db_path, owner_display, contact_display, verified_owner, identity_fixture):
    """Return a fixed status only; supplied identities are synthetic fixtures."""
    if type(memory) is not synthetic_scan_comparison.SyntheticMemory:
        return "NOT_VERIFIED"
    if type(identity_fixture) is not tuple or len(identity_fixture) != 6:
        return "NOT_VERIFIED"
    if not binding_model.synthetic_consistent(*identity_fixture):
        return "NOT_VERIFIED"
    if sys.gettrace() or sys.getprofile() or threading.gettrace() or threading.getprofile():
        return "NOT_VERIFIED"
    if not all(_valid_id(value) for value in (owner_display, contact_display, verified_owner)):
        return "NOT_VERIFIED"
    if owner_display == contact_display:
        return "NOT_VERIFIED"
    if type(db_path) is not str or not os.path.isabs(db_path):
        return "NOT_VERIFIED"
    root = os.path.realpath(Path(__file__).resolve().parents[2] / ".tmp")
    target = os.path.realpath(db_path)
    try:
        if os.path.normcase(os.path.commonpath((root, target))) != os.path.normcase(root):
            return "NOT_VERIFIED"
        if not target.lower().endswith(".db") or os.path.exists(target + "-wal"):
            return "NOT_VERIFIED"
        with open(target, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size < 4096:
                return "NOT_VERIFIED"
            page = source.read(4096)
            if len(page) != 4096 or not _same_stat(before, os.stat(target)):
                return "NOT_VERIFIED"

            def consume(candidate):
                connection = None
                try:
                    import sqlcipher3
                    if not _same_stat(before, os.stat(target)):
                        return "NOT_VERIFIED"
                    connection = sqlcipher3.connect(Path(target).as_uri() + "?mode=ro", uri=True)
                    failed = False
                    try:
                        connection.set_key(b"x'" + candidate.hex().encode("ascii") + b"'")
                    except Exception:
                        failed = True
                    if failed:
                        return "NOT_VERIFIED"
                    if not connection.execute("PRAGMA cipher_version").fetchone():
                        return "NOT_VERIFIED"
                    connection.set_trace_callback(None)
                    connection.execute("PRAGMA query_only=ON")
                    connection.execute("PRAGMA temp_store=MEMORY")
                    if connection.execute("PRAGMA query_only").fetchone() != (1,):
                        return "NOT_VERIFIED"
                    connection.execute("BEGIN")
                    query = "SELECT username FROM main.contact WHERE alias COLLATE BINARY = ? LIMIT 2"
                    owner = connection.execute(query, (owner_display,)).fetchall()
                    contact = connection.execute(query, (contact_display,)).fetchall()
                    if len(owner) != 1 or len(contact) != 1:
                        return "NOT_VERIFIED"
                    self_id, other_id = owner[0][0], contact[0][0]
                    if (self_id != verified_owner or not _valid_id(other_id)
                            or self_id == other_id or other_id.endswith("@chatroom")):
                        return "NOT_VERIFIED"
                    if not _same_stat(before, os.fstat(source.fileno())) or not _same_stat(before, os.stat(target)):
                        return "NOT_VERIFIED"
                    if not binding_model.synthetic_consistent(*identity_fixture):
                        return "NOT_VERIFIED"
                    return "SYNTHETIC_IDENTITIES_MATCHED"
                except Exception:
                    return "NOT_VERIFIED"
                finally:
                    if connection is not None:
                        try:
                            connection.close()
                        except Exception:
                            pass

            result = bounded_single_pass._scan(memory, page, chunk_size=probe._MAX_CHUNK,
                                                consumer=consume)
            return result if result == "SYNTHETIC_IDENTITIES_MATCHED" else "NOT_VERIFIED"
    except Exception:
        return "NOT_VERIFIED"

