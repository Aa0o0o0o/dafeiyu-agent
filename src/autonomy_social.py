"""Owner-only public-interest capture and receipt-backed daily QQ sharing."""

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from .memory_v2.admission import safe_source
from .memory_v2.astrbot_bridge import DELIVERY_GUARD
from .browser.policy import PublicPolicy
from .message_surface import strip_delivery_annotations

MARKER = re.compile(r"\[\[explore:([^\]\r\n]{0,160})\]\]")


def split_interest(value):
    topics = []

    def strip(match):
        topics.append(match.group(1).strip())
        return ""

    # Literal examples in code are not executable suggestions.
    blocks = re.split(r"(```[\s\S]*?```|`[^`\n]*`)", value)
    for i in range(0, len(blocks), 2):
        blocks[i] = MARKER.sub(strip, blocks[i])
        blocks[i] = re.sub(r"\[\[explore:[^\n]*?(?:\]\]|$)", "", blocks[i])
    return "".join(blocks).rstrip(), topics[:1]


class AutonomySocial:
    def __init__(self, autonomy):
        self.a = autonomy
        self.db = autonomy.db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS autonomy_owner_messages(
          id TEXT,scope TEXT,kind TEXT,body TEXT,created REAL,state TEXT,receipt TEXT,hash TEXT,attempted REAL DEFAULT 0,PRIMARY KEY(scope,id));
        CREATE TABLE IF NOT EXISTS autonomy_share_queue(scope TEXT,run TEXT,created REAL,PRIMARY KEY(scope,run));
        CREATE TABLE IF NOT EXISTS autonomy_share_delivery(scope TEXT,day TEXT,state TEXT,receipt TEXT,PRIMARY KEY(scope,day));
        CREATE TABLE IF NOT EXISTS autonomy_share_preferences(scope TEXT PRIMARY KEY,enabled INTEGER);
        CREATE TABLE IF NOT EXISTS autonomy_chat_interests(scope TEXT,topic TEXT,goal TEXT,created REAL,PRIMARY KEY(scope,topic));
        """)

    def global_enabled(self):
        c = self.a.c
        owner_scope = (
            f"{c.config['instance_id']}:{c.config['bot_qq']}:{c.config['owner_qq']}"
        )
        row = self.db.execute(
            "SELECT enabled FROM autonomy_share_preferences WHERE scope=?",
            (owner_scope,),
        ).fetchone()
        return (
            c.config.get("autonomy_extension", {}).get("share_owner") is True
            and c.config["owner_qq"] == "2222222222"
            and (row is None or bool(row[0]))
        )

    def enabled(self):
        return self.global_enabled() and self.a.eligible()

    def recent(self):
        if not self.a.allowed():
            return []
        return self.db.execute(
            "SELECT kind,state,receipt FROM autonomy_owner_messages WHERE scope=? ORDER BY created DESC LIMIT 3",
            (self.a.scope,),
        ).fetchall()

    def recent_delivered(self):
        """Only verified sent messages, within the existing owner-only scope."""
        if not self.a.allowed() or not self.a.eligible():
            return []
        rows = self.db.execute(
            "SELECT body,attempted FROM autonomy_owner_messages WHERE scope=? AND state='sent' AND receipt!='' AND body!='' AND attempted>? ORDER BY attempted DESC LIMIT 3",
            (self.a.scope, time.time() - 7 * 86400),
        ).fetchall()
        return [{"body": body[:300], "sent_at": stamp} for body, stamp in rows]

    def clear(self):
        # Keep the per-day send claim even after forget/restart: no duplicate pushes.
        with self.db:
            self.db.execute(
                "UPDATE autonomy_owner_messages SET body='',state=CASE WHEN state='pending' THEN 'cancelled' ELSE state END WHERE scope=?",
                (self.a.scope,),
            )
            for table in ("autonomy_share_queue", "autonomy_chat_interests"):
                self.db.execute(f"DELETE FROM {table} WHERE scope=?", (self.a.scope,))

    def capture(self, topic):
        if (
            not self.a.allowed()
            or not self.a.eligible()
            or not self.a.state()["enabled"]
            or not self.a.c.config.get("autonomy_extension", {}).get("chat_interests")
        ):
            return False
        if (
            not isinstance(topic, str)
            or not 2 <= len(topic) <= 48
            or not safe_source(topic)
        ):
            return False
        if re.search(
            r'[0-9@:/\\\n\r"“”‘’]|主人|用户|好友|群友|微信|QQ|qq|密码|账号|住址|地址|电话|邮箱|公司|学校|姓名|身份|隐私|聊天|我的|你的|他的|她的',
            topic,
        ):
            return False
        now = time.time()
        with self.db:
            old = self.db.execute(
                "SELECT goal FROM autonomy_chat_interests WHERE scope=? AND topic=?",
                (self.a.scope, topic),
            ).fetchone()
            if old:
                return False
            active = self.db.execute(
                "SELECT i.goal FROM autonomy_chat_interests i JOIN autonomy_goals g ON i.goal=g.id AND i.scope=g.scope WHERE i.scope=? AND g.status='active' ORDER BY i.created",
                (self.a.scope,),
            ).fetchall()
            if len(active) >= 5:
                self.db.execute(
                    "UPDATE autonomy_goals SET status='pause' WHERE scope=? AND id=?",
                    (self.a.scope, active[0][0]),
                )
            from uuid import uuid4

            goal = uuid4().hex
            self.db.execute(
                "INSERT INTO autonomy_goals VALUES(?,?,?,'active',0)",
                (goal, self.a.scope, topic),
            )
            self.db.execute(
                "INSERT INTO autonomy_goal_threads VALUES(?,?,?,'chat_interest',?)",
                (self.a.scope, goal, topic, now),
            )
            self.db.execute(
                "INSERT INTO autonomy_chat_interests VALUES(?,?,?,?)",
                (self.a.scope, topic, goal, now),
            )
        return True

    def expire(self):
        with self.db:
            self.db.execute(
                "UPDATE autonomy_goals SET status='pause' WHERE scope=? AND id IN (SELECT goal FROM autonomy_chat_interests WHERE scope=? AND created<?) AND status='active'",
                (self.a.scope, self.a.scope, time.time() - 7 * 86400),
            )
            self.db.execute(
                "DELETE FROM autonomy_chat_interests WHERE scope=? AND created<?",
                (self.a.scope, time.time() - 7 * 86400),
            )

    def enqueue_message(self, source, body, kind="thought"):
        body = strip_delivery_annotations(body)
        if (
            not self.enabled()
            or not self.a.allowed()
            or not isinstance(body, str)
            or not body.strip()
            or len(body) > 900
            or not safe_source(body)
        ):
            return False
        if kind == "thought" and len(body) > 240:
            return False
        digest = hashlib.sha256(body.strip().encode()).hexdigest()
        if self.db.execute(
            "SELECT 1 FROM autonomy_owner_messages WHERE scope=? AND hash=? AND created>?",
            (self.a.scope, digest, time.time() - 86400),
        ).fetchone():
            return False
        # Ignore punctuation-only rewrites. More speculative semantic matching
        # is left to the model with recent delivered context, so a genuinely new
        # observation is not silently suppressed.
        def normalized(value):
            return "".join(
                c for c in unicodedata.normalize("NFKC", value).casefold()
                if not c.isspace() and not unicodedata.category(c).startswith("P")
            )
        target = normalized(body)
        if any(
            normalized(previous) == target
            for (previous,) in self.db.execute(
                "SELECT body FROM autonomy_owner_messages WHERE scope=? AND body!='' AND created>? ORDER BY created DESC LIMIT 50",
                (self.a.scope, time.time() - 86400),
            )
        ):
            return False
        with self.db:
            result = self.db.execute(
                "INSERT OR IGNORE INTO autonomy_owner_messages VALUES(?,?,?,?,?,'pending','',?,0)",
                (source, self.a.scope, kind, body.strip(), time.time(), digest),
            )
        return bool(result.rowcount)

    def enqueue(self, run):
        if not self.enabled() or not self.a.allowed():
            return
        row = self.db.execute(
            "SELECT reflection,receipt FROM autonomy_runs WHERE scope=? AND id=? AND status='succeeded'",
            (self.a.scope, run),
        ).fetchone()
        if not row:
            return
        try:
            urls = self.sources(row[1])
        except (ValueError, TypeError, AttributeError):
            return
        if urls and row[0] and safe_source(row[0]):
            self.enqueue_message(
                "explore:" + run,
                "刚刚探索时看到一点有意思的，想和你说说～\n"
                + row[0][:240]
                + "\n"
                + urls[0],
                "discovery",
            )

    def validate_packet(self, epoch):
        try:
            self.a.check(epoch)
            c = self.a.c
            return (
                self.enabled()
                and c.verified
                and not c.closed
                and not c.active
                and c.queue.empty()
                and bool(self.a.state()["enabled"])
            )
        except asyncio.CancelledError:
            return False

    @staticmethod
    def sources(receipt):
        urls = []

        def visit(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in {"url", "source_url"} and isinstance(v, str):
                        try:
                            PublicPolicy().url(v)
                            if len(v) <= 512 and v not in urls:
                                urls.append(v)
                        except ValueError:
                            pass
                    elif k == "excerpt" and isinstance(v, str):
                        try:
                            visit(json.loads(v))
                        except ValueError:
                            pass
                    elif isinstance(v, (dict, list)):
                        visit(v)
            elif isinstance(obj, list):
                for v in obj:
                    visit(v)

        # Cite observed result URLs, never planned request URLs.
        for action in json.loads(receipt).get("actions", []):
            visit(action.get("result", {}))
        return urls[:2]

    async def deliver(self):
        epoch = self.a.sync()
        if not self.validate_packet(epoch):
            return False
        # Adopt only previously pending items, never backfill historical experiences.
        for (run,) in self.db.execute(
            "SELECT run FROM autonomy_share_queue WHERE scope=?", (self.a.scope,)
        ).fetchall():
            self.enqueue(run)
        with self.db:
            self.db.execute(
                "DELETE FROM autonomy_share_queue WHERE scope=?", (self.a.scope,)
            )
            self.db.execute(
                "UPDATE autonomy_owner_messages SET state='expired',body='' WHERE scope=? AND state='pending' AND created<?",
                (self.a.scope, time.time() - 86400),
            )
            self.db.execute(
                "UPDATE autonomy_owner_messages SET body='' WHERE scope=? AND state!='pending' AND created<?",
                (self.a.scope, time.time() - 7 * 86400),
            )
        last = self.db.execute(
            "SELECT MAX(attempted) FROM autonomy_owner_messages WHERE scope=?",
            (self.a.scope,),
        ).fetchone()[0]
        if last and time.time() - last < 120:
            return False
        row = self.db.execute(
            "SELECT id,body FROM autonomy_owner_messages WHERE scope=? AND state='pending' ORDER BY created LIMIT 1",
            (self.a.scope,),
        ).fetchone()
        if not row:
            return False
        body = strip_delivery_annotations(row[1])
        if not body.strip():
            with self.db:
                self.db.execute(
                    "UPDATE autonomy_owner_messages SET state='cancelled',body='' WHERE scope=? AND id=?",
                    (self.a.scope, row[0]),
                )
            return False
        with self.db:
            claim = self.db.execute(
                "UPDATE autonomy_owner_messages SET state='sending',attempted=? WHERE scope=? AND id=? AND state='pending'",
                (time.time(), self.a.scope, row[0]),
            )
        if not claim.rowcount:
            return False
        state, receipt = "unknown", ""
        token = DELIVERY_GUARD.set((self, epoch))
        try:
            if not self.validate_packet(epoch):
                state = "cancelled"
                return False
            receipt = await self.a.c.channel.send_text(
                strip_delivery_annotations(row[1])
            )
            state = "sent" if receipt else "unknown"
            return bool(receipt)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        finally:
            DELIVERY_GUARD.reset(token)
            with self.db:
                self.db.execute(
                    "UPDATE autonomy_owner_messages SET state=?,receipt=? WHERE scope=? AND id=?",
                    (state, str(receipt or ""), self.a.scope, row[0]),
                )
