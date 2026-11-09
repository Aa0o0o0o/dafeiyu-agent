"""Opt-in single-conversation local store; no transport, model, or WeChat access.

Caller must bind scope/actor/verified to authenticated QQ session, not model input.
SQLite is plaintext local storage; deleting it is not guaranteed secure erasure.
"""

import hashlib
import json
import math
import re
import sqlite3
import time
from pathlib import Path

from .memory_state import safe_text
from .wechat_message_codec import MAX_TEXT_BYTES, TextChunk, join_text, split_text

UNKNOWN_SENDER = "unknown"


class AccessDenied(PermissionError):
    pass


class StaleSnapshot(RuntimeError):
    pass


def _identifier(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 200
        or value != value.strip()
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("invalid identifier")
    return value


class WeChatPrivateStore:
    """Exactly one pre-verified conversation, with owner-only access and epoch guard."""

    def __init__(
        self,
        project_root,
        *,
        owner_scope,
        owner_qq,
        account_id,
        conversation_id,
        owner_sender_id,
        contact_sender_id,
    ):
        self.owner_qq = _identifier(owner_qq)
        self.owner_scope = _identifier(owner_scope)
        self.account_id = _identifier(account_id)
        self.conversation_id = _identifier(conversation_id)
        self.owner_sender_id = _identifier(owner_sender_id)
        self.contact_sender_id = _identifier(contact_sender_id)
        if (
            owner_qq != "2222222222"
            or ":group:" in owner_scope
            or not owner_scope.endswith(":" + owner_qq)
            or owner_sender_id == contact_sender_id
            or UNKNOWN_SENDER in {owner_sender_id, contact_sender_id}
        ):
            raise ValueError("invalid owner or conversation binding")
        root = Path(project_root).resolve(strict=True)
        directory = root / "runtime" / "owner-private"
        for part in (root / "runtime", directory):
            if part.exists() and (part.is_symlink() or part.is_junction()):
                raise ValueError("invalid storage directory")
            part.mkdir(exist_ok=True)
        if not directory.resolve().is_relative_to(root):
            raise ValueError("storage outside project")
        target = directory / "wechat.sqlite3"
        if target.exists() and (target.is_symlink() or target.is_junction()):
            raise ValueError("invalid storage file")
        self.db = sqlite3.connect(target, timeout=5)
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS wx_private_control(scope TEXT,conversation TEXT,binding_hash TEXT,enabled INTEGER NOT NULL DEFAULT 0,epoch INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(scope,conversation));
        CREATE TABLE IF NOT EXISTS wx_private_batch(scope TEXT,conversation TEXT,batch TEXT,state TEXT,source_kind TEXT NOT NULL DEFAULT 'visible_screen_ocr',PRIMARY KEY(scope,conversation,batch));
        CREATE TABLE IF NOT EXISTS wx_private_message(scope TEXT,conversation TEXT,message TEXT,batch TEXT,sender TEXT,sender_role TEXT,occurred REAL,kind TEXT,body TEXT,redacted INTEGER,PRIMARY KEY(scope,conversation,message));
        CREATE TABLE IF NOT EXISTS wx_private_tombstone(scope TEXT,conversation TEXT,message TEXT,PRIMARY KEY(scope,conversation,message));
        CREATE TABLE IF NOT EXISTS wx_private_chunk(scope TEXT,conversation TEXT,message TEXT,chunk_index INTEGER,chunk_total INTEGER,digest TEXT,body TEXT,PRIMARY KEY(scope,conversation,message,chunk_index));
        CREATE TABLE IF NOT EXISTS wx_private_source_meta(scope TEXT,conversation TEXT,message TEXT,shard TEXT,local_id INTEGER,server_id INTEGER,raw_type INTEGER,created_at INTEGER,image_digest TEXT,image_digest_status TEXT,quote_status TEXT,quote_text TEXT,PRIMARY KEY(scope,conversation,message));
        CREATE INDEX IF NOT EXISTS wx_private_time ON wx_private_message(scope,conversation,occurred);
        """)
        columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(wx_private_batch)")
        }
        if "source_kind" not in columns:
            self.db.execute(
                "ALTER TABLE wx_private_batch ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'visible_screen_ocr'"
            )
        binding = hashlib.sha256(
            json.dumps(
                [self.account_id, self.owner_sender_id, self.contact_sender_id],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        # One owner and one explicitly verified conversation per store. A new
        # source must not silently create another account/conversation binding.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT scope,conversation,binding_hash FROM wx_private_control LIMIT 1"
            ).fetchone()
            if row and row != (self.owner_scope, self.conversation_id, binding):
                raise ValueError("conversation binding mismatch")
            self.created_binding = row is None
            if not row:
                self.db.execute(
                    "INSERT INTO wx_private_control VALUES(?,?,?,0,0)",
                    (self.owner_scope, self.conversation_id, binding),
                )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def _identity(self, scope, actor, verified):
        if scope != self.owner_scope or actor != self.owner_qq or verified is not True:
            raise AccessDenied("access denied")

    def _control(self):
        return self.db.execute(
            "SELECT enabled,epoch FROM wx_private_control WHERE scope=? AND conversation=?",
            (self.owner_scope, self.conversation_id),
        ).fetchone()

    def set_access(self, *, scope, actor, verified, enabled):
        self._identity(scope, actor, verified)
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        with self.db:
            self.db.execute(
                "UPDATE wx_private_control SET enabled=?,epoch=epoch+1 WHERE scope=? AND conversation=? AND enabled!=?",
                (int(enabled), self.owner_scope, self.conversation_id, int(enabled)),
            )
        return self._control()[1]

    def snapshot(self, *, scope, actor, verified):
        self._identity(scope, actor, verified)
        enabled, epoch = self._control()
        if not enabled:
            raise AccessDenied("access disabled")
        return epoch

    def validate_snapshot(self, *, scope, actor, verified, snapshot):
        self._identity(scope, actor, verified)
        enabled, epoch = self._control()
        return bool(enabled and type(snapshot) is int and snapshot == epoch)

    def _guard(self, snapshot):
        enabled, epoch = self._control()
        if not enabled:
            raise AccessDenied("access disabled")
        if type(snapshot) is not int or snapshot != epoch:
            raise StaleSnapshot("source changed")

    def import_batch(
        self,
        *,
        scope,
        actor,
        verified,
        batch_id,
        messages,
        snapshot,
        source_kind="visible_screen_ocr",
    ):
        self._identity(scope, actor, verified)
        batch_id = _identifier(batch_id)
        if source_kind not in ("visible_screen_ocr", "verified_local_db"):
            raise ValueError("invalid source kind")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 500:
            raise ValueError("invalid batch")
        clean = []
        seen = set()
        allowed = {
            "conversation_id",
            "message_id",
            "sender_id",
            "occurred_at",
            "kind",
            "text",
        }
        for m in messages:
            if (
                not isinstance(m, dict)
                or set(m) != allowed
                or m["conversation_id"] != self.conversation_id
            ):
                raise ValueError("invalid message scope")
            mid = _identifier(m["message_id"])
            sender = _identifier(m["sender_id"])
            if mid in seen or sender not in (
                self.owner_sender_id,
                self.contact_sender_id,
                UNKNOWN_SENDER,
            ):
                raise ValueError("invalid message identity")
            seen.add(mid)
            ts = m["occurred_at"]
            kind = m["kind"]
            body = m["text"]
            if (
                (
                    ts is not None
                    and (
                        type(ts) not in (int, float)
                        or not math.isfinite(ts)
                        or not 0 < ts <= time.time() + 300
                    )
                )
                or kind not in {"text", "image", "audio", "video", "other"}
                or not isinstance(body, str)
                or len(body.encode("utf-8")) > MAX_TEXT_BYTES
                or (kind == "text" and not body.strip())
                or (kind != "text" and body)
            ):
                raise ValueError("invalid message")
            redacted = kind == "text" and safe_text(body) is None
            clean.append(
                (
                    mid,
                    sender,
                    (
                        "owner"
                        if sender == self.owner_sender_id
                        else "contact"
                        if sender == self.contact_sender_id
                        else "unknown"
                    ),
                    float(ts) if ts is not None else None,
                    kind,
                    "[敏感内容已排除]" if redacted else body,
                    int(redacted),
                )
            )
        inserted = duplicates = 0
        nested = self.db.in_transaction
        self.db.execute("SAVEPOINT wx_import_batch" if nested else "BEGIN IMMEDIATE")
        try:
            self._guard(snapshot)
            previous = self.db.execute(
                "SELECT state FROM wx_private_batch WHERE scope=? AND conversation=? AND batch=?",
                (self.owner_scope, self.conversation_id, batch_id),
            ).fetchone()
            if previous:
                raise ValueError("batch identifier already used")
            self.db.execute(
                "INSERT INTO wx_private_batch(scope,conversation,batch,state,source_kind) VALUES(?,?,?,'active',?)",
                (self.owner_scope, self.conversation_id, batch_id, source_kind),
            )
            for mid, sender, role, ts, kind, body, redacted in clean:
                if self.db.execute(
                    "SELECT 1 FROM wx_private_tombstone WHERE scope=? AND conversation=? AND message=?",
                    (self.owner_scope, self.conversation_id, mid),
                ).fetchone():
                    raise ValueError("withdrawn source cannot be reimported")
                old = self.db.execute(
                    "SELECT sender,sender_role,occurred,kind,body,redacted FROM wx_private_message WHERE scope=? AND conversation=? AND message=?",
                    (self.owner_scope, self.conversation_id, mid),
                ).fetchone()
                if old:
                    old_body = self._restore_body(mid, old[3], old[4])
                    if old[:4] + (old_body, old[5]) != (
                        sender,
                        role,
                        ts,
                        kind,
                        body,
                        redacted,
                    ):
                        raise ValueError("source changed; explicit update required")
                    duplicates += 1
                    continue
                chunks = split_text(body) if kind == "text" and len(body) > 3000 else ()
                stored_body = "" if chunks else body
                self.db.execute(
                    "INSERT INTO wx_private_message VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        self.owner_scope,
                        self.conversation_id,
                        mid,
                        batch_id,
                        sender,
                        role,
                        ts,
                        kind,
                        stored_body,
                        redacted,
                    ),
                )
                if chunks:
                    self.db.executemany(
                        "INSERT INTO wx_private_chunk VALUES(?,?,?,?,?,?,?)",
                        [
                            (
                                self.owner_scope,
                                self.conversation_id,
                                mid,
                                chunk.index,
                                chunk.total,
                                chunk.digest,
                                chunk.text,
                            )
                            for chunk in chunks
                        ],
                    )
                inserted += 1
            if inserted:
                self.db.execute(
                    "UPDATE wx_private_control SET epoch=epoch+1 WHERE scope=? AND conversation=?",
                    (self.owner_scope, self.conversation_id),
                )
            if nested:
                self.db.execute("RELEASE SAVEPOINT wx_import_batch")
            else:
                self.db.commit()
        except BaseException:
            if nested:
                self.db.execute("ROLLBACK TO SAVEPOINT wx_import_batch")
                self.db.execute("RELEASE SAVEPOINT wx_import_batch")
            else:
                self.db.rollback()
            raise
        return {
            "imported": inserted,
            "duplicates": duplicates,
            "redacted": sum(m[-1] for m in clean),
        }

    def _restore_body(self, message_id, kind, body):
        if kind != "text" or body != "":
            return body
        rows = self.db.execute(
            "SELECT chunk_index,chunk_total,digest,body FROM wx_private_chunk "
            "WHERE scope=? AND conversation=? AND message=? ORDER BY chunk_index",
            (self.owner_scope, self.conversation_id, message_id),
        ).fetchall()
        return join_text(tuple(TextChunk(*row) for row in rows))

    def search(self, *, scope, actor, verified, query, snapshot, limit=10):
        self._identity(scope, actor, verified)
        if (
            not isinstance(query, str)
            or len(query) > 100
            or type(limit) is not int
            or not 1 <= limit <= 20
        ):
            raise ValueError("invalid search")
        self._guard(snapshot)
        cursor = self.db.execute(
            "SELECT m.message,m.batch,m.sender,m.sender_role,m.occurred,CASE WHEN m.kind='other' AND d.quote_status='decoded' THEN 'quote' ELSE m.kind END,CASE WHEN m.kind='other' AND d.quote_status='decoded' THEN d.quote_text ELSE m.body END,m.redacted,b.source_kind FROM wx_private_message m JOIN wx_private_batch b ON b.scope=m.scope AND b.conversation=m.conversation AND b.batch=m.batch LEFT JOIN wx_private_source_meta d ON d.scope=m.scope AND d.conversation=m.conversation AND d.message=m.message WHERE m.scope=? AND m.conversation=? AND b.state='active' ORDER BY (m.occurred IS NOT NULL) DESC,m.occurred DESC,m.rowid DESC",
            (self.owner_scope, self.conversation_id),
        )
        keys = (
            "message_id",
            "batch_id",
            "sender_id",
            "sender_role",
            "occurred_at",
            "kind",
            "text",
            "redacted",
        )
        out = []
        for row in cursor:
            body = self._restore_body(row[0], row[5], row[6])
            if query and query.casefold() not in body.casefold():
                continue
            row = row[:6] + (body,) + row[7:]
            out.append(
                dict(
                    zip(keys, row[:8]),
                    source_kind=row[8],
                    platform="wechat",
                    account_id=self.account_id,
                    conversation_id=self.conversation_id,
                    source_state="untrusted_chat_data",
                )
            )
            if len(out) == limit:
                break
        self._guard(snapshot)
        return out

    @staticmethod
    def query_terms(query, aliases=()):
        # Local lexical hints only; no model or network. Strip generic chat words
        # before character n-grams so the contact's name does not dominate.
        text = query.casefold()
        for alias in aliases:
            if isinstance(alias, str) and alias.strip():
                text = text.replace(alias.casefold(), " ")
        text = re.sub(
            r"大肥鱼|微信聊天记录|聊天记录|微信记录|微信|聊天|我们|她们|他们|对方|以前|之前|曾经|上次|最近|当时|有没有|说过|提过|提到|说的|提及|那些|这段|这个|什么|哪个|哪里|怎么|为什么|是否|是不是|我和|的|了|吗|呢|呀",
            " ",
            text,
        )
        chunks = re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]{2,}", text)
        terms = set()
        for chunk in chunks:
            if re.fullmatch(r"[a-z0-9]+", chunk):
                terms.add(chunk)
            else:
                for n in (2, 3, 4):
                    terms.update(
                        chunk[i : i + n] for i in range(max(0, len(chunk) - n + 1))
                    )
        # Longer terms carry more specificity; bound CPU on pathological queries.
        return sorted(terms, key=lambda term: (-len(term), term))[:64]

    def search_relevant(
        self, *, scope, actor, verified, query, snapshot, aliases=(), limit=20, recent=4
    ):
        """Rank entire imported conversation locally, return bounded source records.

        Lexical matching is not semantic understanding. Remaining slots are filled
        with recent available records; unknown timestamps are never synthesized.
        """
        import heapq

        self._identity(scope, actor, verified)
        if (
            not isinstance(query, str)
            or len(query) > 3000
            or not isinstance(aliases, (tuple, list))
            or len(aliases) > 5
            or type(limit) is not int
            or not 1 <= limit <= 20
            or type(recent) is not int
            or not 0 <= recent <= limit
        ):
            raise ValueError("invalid search")
        self._guard(snapshot)
        terms = self.query_terms(query, aliases)
        ranked = []
        if terms:
            cursor = self.db.execute(
                "SELECT m.message,m.batch,m.sender,m.sender_role,m.occurred,CASE WHEN m.kind='other' AND d.quote_status='decoded' THEN 'quote' ELSE m.kind END,CASE WHEN m.kind='other' AND d.quote_status='decoded' THEN d.quote_text ELSE m.body END,m.redacted,m.rowid,b.source_kind FROM wx_private_message m JOIN wx_private_batch b ON b.scope=m.scope AND b.conversation=m.conversation AND b.batch=m.batch LEFT JOIN wx_private_source_meta d ON d.scope=m.scope AND d.conversation=m.conversation AND d.message=m.message WHERE m.scope=? AND m.conversation=? AND b.state='active' AND ((m.kind='text' AND m.redacted=0) OR (m.kind='other' AND d.quote_status='decoded'))",
                (self.owner_scope, self.conversation_id),
            )
            for row in cursor:
                body = self._restore_body(row[0], row[5], row[6])
                row = row[:6] + (body,) + row[7:]
                content = body.casefold()
                score = sum((len(term) - 1) ** 2 for term in terms if term in content)
                if score:
                    # rowid breaks ties without suggesting it is an event timestamp.
                    item = (score, row[4] is not None, row[4] or 0, row[8], row)
                    if len(ranked) < limit - recent:
                        heapq.heappush(ranked, item)
                    elif ranked and item[:4] > ranked[0][:4]:
                        heapq.heapreplace(ranked, item)
        self._guard(snapshot)
        chosen = [item[4] for item in sorted(ranked, reverse=True)]
        used = {row[0] for row in chosen}
        if len(chosen) < limit:
            cursor = self.db.execute(
                "SELECT m.message,m.batch,m.sender,m.sender_role,m.occurred,CASE WHEN m.kind='other' AND d.quote_status='decoded' THEN 'quote' ELSE m.kind END,CASE WHEN m.kind='other' AND d.quote_status='decoded' THEN d.quote_text ELSE m.body END,m.redacted,m.rowid,b.source_kind FROM wx_private_message m JOIN wx_private_batch b ON b.scope=m.scope AND b.conversation=m.conversation AND b.batch=m.batch LEFT JOIN wx_private_source_meta d ON d.scope=m.scope AND d.conversation=m.conversation AND d.message=m.message WHERE m.scope=? AND m.conversation=? AND b.state='active' ORDER BY (m.occurred IS NOT NULL) DESC,m.occurred DESC,m.rowid DESC LIMIT ?",
                (self.owner_scope, self.conversation_id, limit + len(used)),
            )
            for row in cursor:
                if row[0] not in used:
                    body = self._restore_body(row[0], row[5], row[6])
                    row = row[:6] + (body,) + row[7:]
                    chosen.append(row)
                    used.add(row[0])
                    if len(chosen) == limit:
                        break
        self._guard(snapshot)
        keys = (
            "message_id",
            "batch_id",
            "sender_id",
            "sender_role",
            "occurred_at",
            "kind",
            "text",
            "redacted",
        )
        return [
            dict(
                zip(keys, row[:8]),
                source_kind=row[9],
                platform="wechat",
                account_id=self.account_id,
                conversation_id=self.conversation_id,
                source_state="untrusted_chat_data",
            )
            for row in chosen
        ]

    def timeline_page(self, *, scope, actor, verified, snapshot, offset=0,
                      body_offset=0, limit=120, char_limit=24000):
        """Sequentially expose the complete authorized import in bounded pages."""
        self._identity(scope, actor, verified)
        if (type(offset) is not int or offset < 0 or type(body_offset) is not int
                or body_offset < 0 or type(limit) is not int or not 1 <= limit <= 120
                or type(char_limit) is not int or not 1000 <= char_limit <= 24000):
            raise ValueError("invalid timeline cursor")
        self._guard(snapshot)
        sql = ("SELECT m.message,m.batch,m.sender,m.sender_role,m.occurred,"
               "CASE WHEN m.kind='other' AND d.quote_status='decoded' THEN 'quote' ELSE m.kind END,"
               "CASE WHEN m.kind='other' AND d.quote_status='decoded' THEN d.quote_text ELSE m.body END,"
               "m.redacted,b.source_kind FROM wx_private_message m "
               "JOIN wx_private_batch b ON b.scope=m.scope AND b.conversation=m.conversation AND b.batch=m.batch "
               "LEFT JOIN wx_private_source_meta d ON d.scope=m.scope AND d.conversation=m.conversation AND d.message=m.message "
               "WHERE m.scope=? AND m.conversation=? AND b.state='active' "
               "ORDER BY (m.occurred IS NULL),m.occurred,m.rowid LIMIT ? OFFSET ?")
        rows = self.db.execute(sql, (self.owner_scope, self.conversation_id, limit + 1, offset)).fetchall()
        keys = ('message_id','batch_id','sender_id','sender_role','occurred_at','kind','text','redacted')
        out, chars, index, pos = [], 0, offset, body_offset
        for row in rows[:limit]:
            body = self._restore_body(row[0], row[5], row[6]) or ""
            if pos > len(body):
                raise ValueError("invalid text cursor")
            available = char_limit - chars
            if available <= 0:
                break
            chunk = body[pos:pos + available]
            record = dict(zip(keys, row[:6] + (chunk,) + row[7:8]), source_kind=row[8],
                          selection='timeline', context_group=None)
            record['text_is_partial'] = pos > 0 or pos + len(chunk) < len(body)
            out.append(record)
            chars += len(chunk)
            if pos + len(chunk) < len(body):
                pos += len(chunk)
                break
            index += 1
            pos = 0
        self._guard(snapshot)
        return out, (index, pos), bool(index < offset + len(rows) or pos)

    def history_overview(self, *, scope, actor, verified, snapshot):
        """Aggregate the authorized local import, without returning message bodies."""
        self._identity(scope, actor, verified)
        self._guard(snapshot)
        rows = self.db.execute(
            "SELECT m.kind, COUNT(*), SUM(CASE WHEN m.redacted=1 THEN 1 ELSE 0 END) "
            "FROM wx_private_message m JOIN wx_private_batch b ON b.scope=m.scope "
            "AND b.conversation=m.conversation AND b.batch=m.batch "
            "WHERE m.scope=? AND m.conversation=? AND b.state='active' GROUP BY m.kind",
            (self.owner_scope, self.conversation_id),
        ).fetchall()
        quote_count = self.db.execute(
            "SELECT COUNT(*) FROM wx_private_source_meta d JOIN wx_private_message m "
            "ON m.scope=d.scope AND m.conversation=d.conversation AND m.message=d.message "
            "JOIN wx_private_batch b ON b.scope=m.scope AND b.conversation=m.conversation "
            "AND b.batch=m.batch WHERE m.scope=? AND m.conversation=? "
            "AND b.state='active' AND d.quote_status='decoded'",
            (self.owner_scope, self.conversation_id),
        ).fetchone()[0]
        self._guard(snapshot)
        return {
            "total": sum(row[1] for row in rows),
            "types": {row[0]: row[1] for row in rows},
            "redacted": sum(row[2] or 0 for row in rows),
            "decoded_quotes": quote_count,
        }

    def search_with_context(self, *, scope, actor, verified, query, snapshot, aliases=()):
        """Search the whole local import and include nearby verified messages.

        Timestamp windows are approximate: equal-time ordering may be unknown, and
        selected excerpts are never represented as a complete conversation.
        """
        auth = dict(scope=scope, actor=actor, verified=verified)
        ranked = self.search_relevant(**auth, query=query, snapshot=snapshot,
                                      aliases=aliases, limit=16, recent=4)
        terms = self.query_terms(query, aliases)
        matched = [row for row in ranked if terms and any(
            term in row['text'].casefold() for term in terms
        )]
        anchors = matched[:12] if matched else ranked[:8]
        selected, used = [], set()
        keys = ('message_id', 'batch_id', 'sender_id', 'sender_role',
                'occurred_at', 'kind', 'text', 'redacted')
        sql = ("SELECT m.message,m.batch,m.sender,m.sender_role,m.occurred,"
               "CASE WHEN m.kind='other' AND d.quote_status='decoded' THEN 'quote' ELSE m.kind END,"
               "CASE WHEN m.kind='other' AND d.quote_status='decoded' THEN d.quote_text ELSE m.body END,"
               "m.redacted,m.rowid,b.source_kind FROM wx_private_message m "
               "JOIN wx_private_batch b ON b.scope=m.scope AND b.conversation=m.conversation AND b.batch=m.batch "
               "LEFT JOIN wx_private_source_meta d ON d.scope=m.scope AND d.conversation=m.conversation AND d.message=m.message "
               "WHERE m.scope=? AND m.conversation=? AND b.state='active' AND b.source_kind='verified_local_db' "
               "AND m.occurred IS NOT NULL AND m.occurred BETWEEN ? AND ? ")
        for group, anchor in enumerate(anchors, 1):
            neighbors = []
            stamp = anchor['occurred_at']
            if matched and stamp is not None and anchor['source_kind'] == 'verified_local_db':
                rowid = self.db.execute(
                    "SELECT rowid FROM wx_private_message WHERE scope=? AND conversation=? AND message=?",
                    (self.owner_scope, self.conversation_id, anchor['message_id']),
                ).fetchone()
                if rowid:
                    for op, order in (('<', 'DESC'), ('>', 'ASC')):
                        statement = (sql + f"AND (m.occurred {op} ? OR (m.occurred = ? AND m.rowid {op} ?)) "
                                     + f"ORDER BY m.occurred {order},m.rowid {order} LIMIT 2")
                        args = (self.owner_scope, self.conversation_id, stamp-600, stamp+600,
                                stamp, stamp, rowid[0])
                        for row in self.db.execute(statement, args):
                            body = self._restore_body(row[0], row[5], row[6])
                            row = row[:6] + (body,) + row[7:]
                            neighbors.append(dict(zip(keys, row[:8]), source_kind=row[9],
                                                  platform='wechat', account_id=self.account_id,
                                                  conversation_id=self.conversation_id,
                                                  source_state='untrusted_chat_data'))
            group_rows = [(r, 'nearby') for r in neighbors] + [(anchor, 'match' if matched else 'recent')]
            group_rows.sort(key=lambda item: (item[0]['occurred_at'] is None,
                                              item[0]['occurred_at'] or 0))
            for row, relation in group_rows:
                if row['message_id'] in used or len(selected) >= 48:
                    continue
                used.add(row['message_id'])
                selected.append(dict(row, context_group=group, selection=relation))
        self._guard(snapshot)
        return selected

    def revoke_batch(self, *, scope, actor, verified, batch_id, snapshot):
        self._identity(scope, actor, verified)
        batch_id = _identifier(batch_id)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._guard(snapshot)
            row = self.db.execute(
                "SELECT state FROM wx_private_batch WHERE scope=? AND conversation=? AND batch=?",
                (self.owner_scope, self.conversation_id, batch_id),
            ).fetchone()
            if not row or row[0] != "active":
                raise ValueError("batch not active")
            self.db.execute(
                "INSERT OR IGNORE INTO wx_private_tombstone SELECT scope,conversation,message FROM wx_private_message WHERE scope=? AND conversation=? AND batch=?",
                (self.owner_scope, self.conversation_id, batch_id),
            )
            self.db.execute(
                "DELETE FROM wx_private_chunk WHERE scope=? AND conversation=? AND message IN (SELECT message FROM wx_private_message WHERE scope=? AND conversation=? AND batch=?)",
                (
                    self.owner_scope,
                    self.conversation_id,
                    self.owner_scope,
                    self.conversation_id,
                    batch_id,
                ),
            )
            self.db.execute(
                "DELETE FROM wx_private_source_meta WHERE scope=? AND conversation=? AND message IN (SELECT message FROM wx_private_message WHERE scope=? AND conversation=? AND batch=?)",
                (self.owner_scope, self.conversation_id, self.owner_scope, self.conversation_id, batch_id),
            )
            result = self.db.execute(
                "DELETE FROM wx_private_message WHERE scope=? AND conversation=? AND batch=?",
                (self.owner_scope, self.conversation_id, batch_id),
            )
            self.db.execute(
                "UPDATE wx_private_batch SET state='revoked' WHERE scope=? AND conversation=? AND batch=?",
                (self.owner_scope, self.conversation_id, batch_id),
            )
            self.db.execute(
                "UPDATE wx_private_control SET epoch=epoch+1 WHERE scope=? AND conversation=?",
                (self.owner_scope, self.conversation_id),
            )
            self.db.commit()
            return result.rowcount
        except BaseException:
            self.db.rollback()
            raise

    def clear(self, *, scope, actor, verified, snapshot):
        self._identity(scope, actor, verified)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._guard(snapshot)
            self.db.execute(
                "INSERT OR IGNORE INTO wx_private_tombstone SELECT scope,conversation,message FROM wx_private_message WHERE scope=? AND conversation=?",
                (self.owner_scope, self.conversation_id),
            )
            self.db.execute(
                "DELETE FROM wx_private_chunk WHERE scope=? AND conversation=?",
                (self.owner_scope, self.conversation_id),
            )
            self.db.execute(
                "DELETE FROM wx_private_source_meta WHERE scope=? AND conversation=?",
                (self.owner_scope, self.conversation_id),
            )
            result = self.db.execute(
                "DELETE FROM wx_private_message WHERE scope=? AND conversation=?",
                (self.owner_scope, self.conversation_id),
            )
            self.db.execute(
                "UPDATE wx_private_batch SET state='revoked' WHERE scope=? AND conversation=?",
                (self.owner_scope, self.conversation_id),
            )
            self.db.execute(
                "UPDATE wx_private_control SET epoch=epoch+1 WHERE scope=? AND conversation=?",
                (self.owner_scope, self.conversation_id),
            )
            self.db.commit()
            return result.rowcount
        except BaseException:
            self.db.rollback()
            raise
