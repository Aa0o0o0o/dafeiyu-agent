"""Synthetic-only demonstration of same-process key handoff.

This module cannot open a real process and must not be wired into production.
The caller must supply a synthetic database and synthetic process-memory bytes.
"""
from __future__ import annotations

import hashlib
import os
import sys
import threading
from pathlib import Path

if __package__:
    from . import probe
else:
    import probe


class SyntheticMemory:
    """A data-only synthetic process image, not a process-reading adapter."""
    def __init__(self, block: bytes):
        if type(block) is not bytes or len(block) > 1048576:
            raise ValueError("A bounded synthetic block is required")
        self._block = block

    def regions(self):
        yield 0x10000, len(self._block)

    def read(self, address, size):
        offset = address - 0x10000
        if offset < 0 or size < 0 or offset + size > len(self._block):
            return None
        return self._block[offset:offset + size]


def _guard():
    if sys.gettrace() or sys.getprofile() or threading.gettrace() or threading.getprofile():
        raise RuntimeError("Tracing must be disabled")


def read_synthetic_page(*, memory: SyntheticMemory, db_path: str, username: str,
                        limit: int = 100, offset: int = 0) -> tuple:
    """Return one fake target table page, without returning the connection/key."""
    _guard()
    if type(memory) is not SyntheticMemory or type(memory._block) is not bytes:
        raise TypeError("Synthetic data source required")
    if type(db_path) is not str or not os.path.isabs(db_path):
        raise ValueError("Explicit synthetic database path required")
    root = os.path.realpath(Path(__file__).resolve().parents[2] / ".tmp")
    target = os.path.realpath(db_path)
    try:
        inside = os.path.normcase(os.path.commonpath((root, target))) == os.path.normcase(root)
    except ValueError:
        inside = False
    if not inside or target == root or not target.lower().endswith(".db"):
        raise ValueError("Synthetic database must be under the project test directory")
    if type(username) is not str or not username or username.strip() != username or chr(0) in username or username.endswith("@chatroom"):
        raise ValueError("One synthetic private username required")
    if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
        raise ValueError("Invalid paging")
    with open(target, "rb") as file:
        page = file.read(4096)
    if len(page) != 4096:
        raise ValueError("Invalid synthetic encrypted page")
    table = "Msg_" + hashlib.md5(username.encode(), usedforsecurity=False).hexdigest()
    connection, candidate = None, None
    try:
        candidate = probe._find_page_key(memory, page)
        if not candidate:
            raise RuntimeError("No synthetic candidate verified")
        import sqlcipher3
        connection = sqlcipher3.connect(Path(target).as_uri() + "?mode=ro", uri=True)
        failed = False
        try:
            connection.set_key(b"x'" + candidate.hex().encode("ascii") + b"'")
        except Exception:
            failed = True
        finally:
            candidate = None
        if failed:
            raise RuntimeError("Cannot establish synthetic encrypted connection")
        if not connection.execute("PRAGMA cipher_version").fetchone():
            raise RuntimeError("SQLCipher unavailable")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        if not connection.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=? COLLATE BINARY", (table,)).fetchone():
            raise ValueError("Selected synthetic conversation absent")
        sql = (f'SELECT local_id,local_type,server_id,real_sender_id,create_time,'
               f'message_content,packed_info_data,sort_seq FROM main."{table}" '
               f'ORDER BY sort_seq,create_time,local_id LIMIT ? OFFSET ?')
        plan = connection.execute("EXPLAIN QUERY PLAN " + sql, (limit, offset))
        if any("USE TEMP B-TREE" in str(row[3]).upper() for row in plan):
            raise ValueError("Selected table requires an ordering index")
        return tuple(connection.execute(sql, (limit, offset)).fetchall())
    finally:
        candidate = None
        if connection is not None:
            connection.close()
