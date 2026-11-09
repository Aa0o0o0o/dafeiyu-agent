"""Synthetic-only, single-conversation snapshot reader; not a WeChat adapter.

Caller supplies trusted connections to all coherent snapshots and the verified
exact username. This module never opens files, obtains keys, or starts imports.
"""

from __future__ import annotations

import hashlib
import heapq
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Iterator, Protocol, Sequence


class ReadConnection(Protocol):
    in_transaction: bool

    def execute(self, sql: str, parameters: tuple = ()) -> Any: ...

    def rollback(self) -> None: ...


class SenderLookup(Protocol):
    """Lazy rowid lookup, using the shard connection's pinned read transaction."""

    def get(self, rowid: int | None) -> str | None: ...


@dataclass(frozen=True)
class Shard:
    name: str
    connection: ReadConnection
    sender_names: SenderLookup | None = None


@dataclass(frozen=True)
class Message:
    shard: str
    local_id: int
    local_type: int
    server_id: int
    real_sender_id: int | None
    create_time: int
    message_content: str | bytes | None
    packed_info_data: str | bytes | None
    sort_seq: int
    compress_content: str | bytes | None = None
    has_compress_content: bool = False


class AtomicStage(Protocol):
    """Trusted sink must implement a real all-or-nothing storage transaction."""

    def begin(self) -> None: ...

    def write_page(self, page: tuple[Message, ...]) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def conversation_table(username: str) -> str:
    if not isinstance(username, str) or not username or username != username.strip():
        raise ValueError('an exact nonempty username is required')
    if '\x00' in username or username.endswith('@chatroom'):
        raise ValueError('only one individual conversation is accepted')
    return 'Msg_' + hashlib.md5(username.encode('utf-8'), usedforsecurity=False).hexdigest()


def _fingerprint(msg: Message) -> bytes:
    """Store only an identity digest, never retain message bodies for dedup."""
    digest = hashlib.sha256()
    for value in (msg.local_type, msg.create_time, msg.message_content, msg.packed_info_data,
                  msg.sort_seq, msg.compress_content, int(msg.has_compress_content)):
        if value is None:
            tag, raw = b'n', b''
        elif type(value) is int:
            tag, raw = b'i', str(value).encode('ascii')
        elif isinstance(value, str):
            tag, raw = b's', value.encode('utf-8')
        elif isinstance(value, bytes):
            tag, raw = b'b', value
        else:
            raise ValueError('unsupported content type')
        digest.update(tag + len(raw).to_bytes(8, 'big'))
        digest.update(raw)
    return digest.digest()


def read_pages(shards: Sequence[Shard], username: str, page_size: int = 100) -> Iterator[tuple[Message, ...]]:
    """Yield tentative pages only: an error may occur after earlier pages.

    Use stage_all and a genuinely atomic trusted sink to publish results. Caller
    supplies complete and mutually coherent snapshots including committed WAL.
    Transactions pin each shard sequentially, not a cross-database atomic snapshot.
    Uses indexed order only when every participating shard can satisfy it without
    a temporary sort. Otherwise streams each shard without ORDER BY; source times
    are retained, but traversal across shards is not chronological. No temporary
    sorting table is created in either path.
    Memory is O(shards + page_size + distinct nonzero server ids), aside from
    SQLite caches and one potentially large row per shard.
    """
    table = conversation_table(username)
    if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 10000:
        raise ValueError('invalid page size')
    if not shards or len({s.name for s in shards}) != len(shards):
        raise ValueError('nonempty uniquely named shards required')
    if any(not s.name or not callable(getattr(s.connection, 'execute', None))
           or not callable(getattr(s.connection, 'rollback', None))
           or not hasattr(s.connection, 'in_transaction') for s in shards):
        raise ValueError('invalid shard')
    if len({id(s.connection) for s in shards}) != len(shards):
        raise ValueError('each shard requires a distinct connection')
    if any(s.connection.in_transaction for s in shards):
        raise ValueError('connections must not already have open transactions')
    columns = ('local_id', 'local_type', 'server_id', 'real_sender_id', 'create_time',
               'message_content', 'packed_info_data', 'sort_seq')
    started: list[ReadConnection] = []
    cursors: list[tuple[Shard, Any]] = []
    prepared: list[tuple[Shard, str, str]] = []
    unordered = False
    heap: list[tuple[int, int, str, int, int, Message]] = []
    by_server: dict[int, tuple[bytes, str | None]] = {}
    page: list[Message] = []
    try:
        for shard in shards:
            conn = shard.connection
            conn.execute('BEGIN')
            started.append(conn)
            # Restrict metadata and reads to main; a TEMP shadow must not redirect us.
            exists = conn.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=? COLLATE BINARY", (table,)).fetchone()
            if not exists:
                continue
            schema = {row[1] for row in conn.execute(f'PRAGMA main.table_info("{table}")')}
            if not set(columns).issubset(schema):
                raise ValueError('unsupported message table schema')
            # Treat missing optional source columns explicitly; do not conflate
            # an absent column with a present column containing NULL.
            compressed = '"compress_content"' if 'compress_content' in schema else 'NULL'
            present = 1 if 'compress_content' in schema else 0
            plain = (f'SELECT {", ".join(columns)}, {compressed}, {present} '
                     f'FROM main."{table}"')
            ordered = plain + ' ORDER BY sort_seq, create_time, local_id'
            plan = conn.execute('EXPLAIN QUERY PLAN ' + ordered)
            if any('USE TEMP B-TREE' in str(row[3]).upper() for row in plan):
                unordered = True
            prepared.append((shard, plain, ordered))
        if not prepared:
            raise ValueError('target conversation not found')
        for shard, plain, ordered in prepared:
            query = plain if unordered else ordered
            # Never issue an unordered scan if SQLite reports a temporary sort.
            if unordered and any('USE TEMP B-TREE' in str(row[3]).upper()
                                 for row in shard.connection.execute('EXPLAIN QUERY PLAN ' + query)):
                raise ValueError('source scan unexpectedly requires temporary sort')
            cursors.append((shard, shard.connection.execute(query)))

        def next_message(index: int) -> Message | None:
            shard, cursor = cursors[index]
            row = cursor.fetchone()
            if row is None:
                return None
            msg = Message(shard.name, *row[:-1], bool(row[-1]))
            if any(type(v) is not int for v in (msg.local_id, msg.local_type, msg.server_id, msg.create_time, msg.sort_seq)):
                raise ValueError('invalid message identifier or ordering field')
            if msg.server_id < 0:
                raise ValueError('invalid server identifier')
            if msg.real_sender_id is not None and type(msg.real_sender_id) is not int:
                raise ValueError('invalid shard-local sender identifier')
            return msg

        def iter_messages():
            if unordered:
                for index in range(len(cursors)):
                    while (msg := next_message(index)) is not None:
                        yield index, msg
                return
            def advance(index):
                msg = next_message(index)
                if msg is not None:
                    heapq.heappush(heap, (msg.sort_seq, msg.create_time, msg.shard, msg.local_id, index, msg))
            for index in range(len(cursors)):
                advance(index)
            while heap:
                _, _, _, _, index, msg = heapq.heappop(heap)
                advance(index)
                yield index, msg

        for index, msg in iter_messages():
            if msg.server_id:
                digest = _fingerprint(msg)
                shard, _ = cursors[index]
                sender = (shard.sender_names.get(msg.real_sender_id)
                          if shard.sender_names is not None else None)
                if not isinstance(sender, str) or not sender:
                    sender = None
                prev = by_server.get(msg.server_id)
                if prev is not None:
                    if prev[0] != digest:
                        raise ValueError('conflicting cross-shard server identifier')
                    if sender is None or prev[1] is None or sender != prev[1]:
                        raise ValueError('duplicate sender identity unresolved or conflicting')
                    continue
                by_server[msg.server_id] = (digest, sender)
            page.append(msg)
            if len(page) == page_size:
                yield tuple(page)
                page.clear()
        if page:
            yield tuple(page)
    finally:
        rollback_error: Exception | None = None
        for conn in reversed(started):
            try:
                conn.rollback()
            except Exception as error:
                rollback_error = rollback_error or error
        if rollback_error is not None:
            raise rollback_error


def stage_all(shards: Sequence[Shard], username: str, sink: AtomicStage, page_size: int = 100) -> None:
    """Publish only after all pages succeed; caller must provide an atomic sink.

    This orchestrator cannot guarantee sink durability/atomicity: a sink that
    exposes uncommitted writes or fails to roll back is unsafe and unsupported.
    """
    try:
        sink.begin()
        with closing(read_pages(shards, username, page_size)) as pages:
            for page in pages:
                sink.write_page(page)
        sink.commit()
    except BaseException:
        sink.rollback()
        raise
