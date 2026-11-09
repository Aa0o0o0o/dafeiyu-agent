"""Synthetic-only staging of one already-identified WeChat conversation.

No WeChat discovery, key handling, file opening, logging, CLI, or bot routing.
Callers supply trusted query-only connections and a verified owner identity;
this module by itself does not verify a live process or real source provenance.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .wechat_import_fence import ImportFence, ImportToken
from .wechat_message_codec import decode_message
from .wechat_private_store import UNKNOWN_SENDER
from .wechat_single_session import Shard, stage_all


@dataclass(frozen=True)
class SyntheticShard:
    name: str
    connection: object


def _query_only(conn):
    if conn.in_transaction or conn.execute("PRAGMA query_only").fetchone() != (1,):
        raise ValueError("source must be query-only and outside a transaction")


def _valid_identifier(value):
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 200
        and not any(ord(c) < 32 for c in value)
    )


def _resolve_unique_contact(
    conn, label, verified_owner, owner_display_id, target_display_id
):
    """Resolve display IDs exactly; caller independently authenticates owner.

    Source-field meanings and live identity are not verified by this function.
    The two lookups use one contact snapshot, not a cross-database snapshot.
    """
    _query_only(conn)
    if not all(
        _valid_identifier(v)
        for v in (verified_owner, owner_display_id, target_display_id)
    ) or (label is not None and not _valid_identifier(label)):
        raise ValueError("exact identity fields required")
    if owner_display_id == target_display_id:
        raise ValueError("owner and target display identities must differ")
    query = (
        "SELECT username,alias,nick_name,remark FROM main.contact "
        "WHERE alias COLLATE BINARY = ? OR username COLLATE BINARY = ?"
    )
    conn.execute("BEGIN")
    try:
        own_rows = conn.execute(query, (owner_display_id, owner_display_id)).fetchall()
        if len(own_rows) != 1 or own_rows[0][0] != verified_owner:
            raise ValueError("account identity mismatch or ambiguous")
        rows = conn.execute(query, (target_display_id, target_display_id)).fetchall()
        if len(rows) != 1 or not _valid_identifier(rows[0][0]):
            raise ValueError("contact not unique")
        target, _, nickname, remark = rows[0]
        if (
            target == verified_owner
            or target.endswith("@chatroom")
            or target == UNKNOWN_SENDER
        ):
            raise ValueError("invalid individual contact")
        if label is not None and label not in (nickname, remark):
            raise ValueError("contact label mismatch")
        return target
    finally:
        conn.rollback()


class _SnapshotNames:
    """Only resolve the owner or target, from the shard's pinned snapshot."""

    def __init__(self, conn, owner, contact):
        self.conn, self.owner, self.contact = conn, owner, contact

    def get(self, sender_id, default=None):
        if type(sender_id) is not int:
            return default
        row = self.conn.execute(
            "SELECT user_name FROM main.Name2Id WHERE rowid=? AND user_name IN (?,?)",
            (sender_id, self.owner, self.contact),
        ).fetchone()
        return row[0] if row else default


class _AtomicStoreStage:
    def __init__(self, store, auth, epoch, batch_id, maps, fence, token):
        self.store, self.auth, self.epoch = store, auth, epoch
        self.batch_id, self.maps = batch_id, maps
        self.fence, self.token = fence, token
        self.started = False
        self.count = 0
        self.imported = 0
        self.duplicates = 0
        self.redacted = 0
        self.media_uninspected = 0

    def begin(self):
        self.fence.check(self.token)
        self.store._identity(**self.auth)
        if self.store.db.in_transaction:
            raise ValueError("destination transaction already active")
        self.store.db.execute("BEGIN IMMEDIATE")
        self.started = True
        self.store._guard(self.epoch)

    def write_page(self, page):
        self.fence.check(self.token)
        records = []
        for msg in page:
            sender = self.maps[msg.shard].get(msg.real_sender_id, UNKNOWN_SENDER)
            decoded = decode_message(
                msg.local_type,
                msg.message_content,
                msg.packed_info_data,
                msg.compress_content,
            )
            kind = (
                decoded.kind
                if decoded.kind in {"text", "image", "audio", "video"}
                else "other"
            )
            body = decoded.text if kind == "text" else ""
            if kind != "text":
                self.media_uninspected += 1
            identity = (
                f"server:{msg.server_id}"
                if msg.server_id
                else "local:"
                + hashlib.sha256(
                    (msg.shard + "\\0" + str(msg.local_id)).encode("utf-8")
                ).hexdigest()
            )
            records.append(
                {
                    "conversation_id": self.store.conversation_id,
                    "message_id": identity,
                    "sender_id": sender,
                    "occurred_at": msg.create_time,
                    "kind": kind,
                    "text": body,
                }
            )
        result = self.store.import_batch(
            **self.auth,
            batch_id=f"{self.batch_id}:{self.count}",
            messages=records,
            snapshot=self.epoch,
            source_kind="verified_local_db",
        )
        self.imported += result["imported"]
        self.duplicates += result["duplicates"]
        self.redacted += result["redacted"]
        self.epoch = self.store.snapshot(**self.auth)
        self.count += 1
        self.fence.check(self.token)

    def commit(self):
        self.fence.publish(self.token, self.store.db.commit)
        self.started = False

    def rollback(self):
        if self.started:
            self.store.db.rollback()
            self.started = False


def import_single_synthetic_session(
    *,
    contact_conn,
    shards,
    contact_label,
    verified_owner,
    owner_display_id,
    target_display_id,
    store,
    auth,
    snapshot,
    batch_id,
    fence,
    token,
    page_size=100,
):
    """Stage exactly one contact into an atomic store; synthetic sources only.

    verified_owner must be independently established by the caller. Merely
    passing a string does not authenticate a WeChat process. Contact and shard
    snapshots are separate; there is no claimed cross-database atomic snapshot.
    """
    if not isinstance(fence, ImportFence) or not isinstance(token, ImportToken):
        raise ValueError("explicit import fence and token required")
    fence.check(token)
    destination = store.db.execute("PRAGMA database_list").fetchone()[2]
    if (
        not destination
        or Path(destination).resolve().parent != fence.path.parent.resolve()
    ):
        raise ValueError("fence and destination mismatch")
    if not isinstance(batch_id, str) or not 1 <= len(batch_id) <= 180:
        raise ValueError("invalid batch id")
    if type(page_size) is not int or not 1 <= page_size <= 500:
        raise ValueError("invalid page size")
    store._identity(**auth)
    store._guard(snapshot)
    target = _resolve_unique_contact(
        contact_conn, contact_label, verified_owner, owner_display_id, target_display_id
    )
    if (
        store.account_id != verified_owner
        or store.owner_sender_id != verified_owner
        or store.contact_sender_id != target
        or store.conversation_id != target
    ):
        raise ValueError("destination identity mismatch")
    if not shards or len({s.name for s in shards}) != len(shards):
        raise ValueError("unique source shards required")
    maps, inputs = {}, []
    for shard in shards:
        _query_only(shard.connection)
        if not isinstance(shard.name, str) or not shard.name:
            raise ValueError("invalid shard")
        names = _SnapshotNames(shard.connection, verified_owner, target)
        maps[shard.name] = names
        inputs.append(Shard(shard.name, shard.connection, names))
    sink = _AtomicStoreStage(store, auth, snapshot, batch_id, maps, fence, token)
    stage_all(inputs, target, sink, page_size=page_size)
    return {
        "imported": sink.imported,
        "duplicates": sink.duplicates,
        "redacted": sink.redacted,
        "media_uninspected": sink.media_uninspected,
        "pages": sink.count,
    }
