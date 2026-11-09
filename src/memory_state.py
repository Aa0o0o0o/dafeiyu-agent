"""Scoped, additive SQLite memory, deletion epochs and bounded role-affect state."""

import math
import re
import sqlite3
import time
from contextlib import nullcontext

DAY = 86400
BASELINE = (0.15, 0.35, 0.65)
PREDICATES = {"nickname", "reply_style", "likes", "dislikes", "project", "episode"}
SENSITIVE = re.compile(
    r"身份证|银行卡|住址|病史|性取向|宗教|\b1[3-9]\d{9}\b",
    re.I,
)
CREDENTIAL = re.compile(
    r"sk-[A-Za-z0-9_-]{10,}|Bearer\s+[A-Za-z0-9._-]{12,}|(?:密码|验证码|api.?key|password|token|令牌)\s*(?:[:=：]|是|为)\s*\S+",
    re.I,
)
NONFACT = re.compile(
    r"开玩笑|假如|假设|比如|引用|他说|她说|如果|不是我|段子|测试|示例|小说|角色扮演"
)


def safe_text(text):
    return None if SENSITIVE.search(text) or CREDENTIAL.search(text) else text


def strength_at(strength, anchor, half_life_days, now):
    return strength * 2 ** (-max(0, now - anchor) / (half_life_days * DAY))


class MemoryState:
    def __init__(self, db, scope):
        self.db, self.scope = db, scope
        db.executescript("""
            CREATE TABLE IF NOT EXISTS ltm_schema(version INTEGER PRIMARY KEY);
            INSERT OR IGNORE INTO ltm_schema VALUES(1);
            CREATE TABLE IF NOT EXISTS ltm_controls(
                scope TEXT PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0, cursor INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS ltm_journal(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, event_id TEXT,
                role TEXT, body TEXT, created REAL, UNIQUE(scope,event_id,role));
            CREATE INDEX IF NOT EXISTS ltm_journal_scope ON ltm_journal(scope,seq);
            CREATE TABLE IF NOT EXISTS ltm_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, predicate TEXT,
                value TEXT, quote TEXT, confidence REAL, importance REAL,
                strength REAL, anchor REAL, half_life REAL, created REAL,
                expires REAL, status TEXT, last_recalled REAL);
            CREATE INDEX IF NOT EXISTS ltm_items_scope ON ltm_items(scope,status);
            CREATE TABLE IF NOT EXISTS ltm_sources(
                scope TEXT, memory_id INTEGER, event_id TEXT,
                PRIMARY KEY(scope,memory_id,event_id));
            CREATE TABLE IF NOT EXISTS ltm_jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, day TEXT,
                epoch INTEGER, end_seq INTEGER, state TEXT, created REAL,
                UNIQUE(scope,day));
            CREATE TABLE IF NOT EXISTS ltm_tombstones(
                scope TEXT, memory_id INTEGER, epoch INTEGER, deleted REAL,
                PRIMARY KEY(scope,memory_id));
            CREATE TABLE IF NOT EXISTS ltm_corrections(
                scope TEXT, old_memory_id INTEGER, explicit_memory_id INTEGER,
                created REAL, PRIMARY KEY(scope,old_memory_id));
            CREATE TABLE IF NOT EXISTS ltm_affect(
                scope TEXT PRIMARY KEY, valence REAL, arousal REAL, energy REAL, anchor REAL);
        """)
        with db:
            db.execute("INSERT OR IGNORE INTO ltm_controls(scope) VALUES(?)", (scope,))
            db.execute(
                "UPDATE ltm_jobs SET state='interrupted' WHERE scope=? AND state='running'",
                (scope,),
            )

    def controls(self):
        row = self.db.execute(
            "SELECT epoch,paused,cursor FROM ltm_controls WHERE scope=?", (self.scope,)
        ).fetchone()
        return {"epoch": row[0], "paused": bool(row[1]), "cursor": row[2]}

    def observe(self, event_id, role, text, now=None):
        now = time.time() if now is None else now
        if self.controls()["paused"]:
            return False
        body = safe_text(text)
        with self.db:
            result = self.db.execute(
                "INSERT OR IGNORE INTO ltm_journal(scope,event_id,role,body,created) VALUES(?,?,?,?,?)",
                (self.scope, event_id, role, body, now),
            )
            if result.rowcount and role == "user" and body:
                self.update_affect(body, now)
        return result.rowcount == 1

    def affect(self, now=None):
        now = time.time() if now is None else now
        row = self.db.execute(
            "SELECT valence,arousal,energy,anchor FROM ltm_affect WHERE scope=?",
            (self.scope,),
        ).fetchone()
        if row is None:
            values = BASELINE
        else:
            decay = 2 ** (-max(0, now - row[3]) / 10800)
            values = tuple(b + (v - b) * decay for b, v in zip(BASELINE, row[:3]))
        return dict(zip(("valence", "arousal", "energy"), values))

    def update_affect(self, text, now):
        values = list(self.affect(now).values())
        # An observed speaker's difficulty guides expression, not a diagnosis or
        # automatic claim that the character shares the speaker's feelings.
        from .conversation_state import appraise

        delta = appraise(text)["delta"]
        limits = ((-1, 1), (0, 1), (0, 1))
        values = [
            max(lo, min(hi, value + max(-0.1, min(0.1, change))))
            for value, change, (lo, hi) in zip(values, delta, limits)
        ]
        self.db.execute(
            "INSERT OR REPLACE INTO ltm_affect VALUES(?,?,?,?,?)",
            (self.scope, *values, now),
        )

    def pending_count(self):
        control = self.controls()
        if control["paused"]:
            return 0
        return self.db.execute(
            "SELECT COUNT(*) FROM ltm_journal WHERE scope=? AND role='user' AND body IS NOT NULL AND seq>?",
            (self.scope, control["cursor"]),
        ).fetchone()[0]

    def batch(self, limit=30):
        control = self.controls()
        if control["paused"]:
            return []
        rows = self.db.execute(
            "SELECT seq,event_id,body,created FROM ltm_journal WHERE scope=? AND role='user' AND body IS NOT NULL AND seq>? ORDER BY seq LIMIT ?",
            (self.scope, control["cursor"], limit),
        ).fetchall()
        return [
            {"seq": seq, "event_id": eid, "text": body, "occurred_at": stamp}
            for seq, eid, body, stamp in rows
        ]

    def can_extract(self, day):
        return (
            not self.controls()["paused"]
            and self.db.execute(
                "SELECT 1 FROM ltm_jobs WHERE scope=? AND day=?", (self.scope, day)
            ).fetchone()
            is None
        )

    def begin_job(self, day, batch, now=None):
        if not batch or not self.can_extract(day):
            return None
        now = time.time() if now is None else now
        epoch = self.controls()["epoch"]
        with self.db:
            try:
                row = self.db.execute(
                    "INSERT INTO ltm_jobs(scope,day,epoch,end_seq,state,created) VALUES(?,?,?,?,?,?)",
                    (self.scope, day, epoch, batch[-1]["seq"], "running", now),
                )
            except sqlite3.IntegrityError:
                return None
            self.db.execute(
                "UPDATE ltm_controls SET cursor=? WHERE scope=?",
                (batch[-1]["seq"], self.scope),
            )
        return row.lastrowid, epoch

    def finish_job(self, job_id, state):
        with self.db:
            self.db.execute(
                "UPDATE ltm_jobs SET state=? WHERE scope=? AND id=?",
                (state, self.scope, job_id),
            )

    def apply_candidates(self, payload, batch, epoch, now=None):
        now = time.time() if now is None else now
        control = self.controls()
        if control["paused"] or epoch != control["epoch"]:
            return 0
        if (
            not isinstance(payload, dict)
            or set(payload) != {"candidates"}
            or not isinstance(payload["candidates"], list)
            or len(payload["candidates"]) > 8
        ):
            raise ValueError("Invalid memory candidate envelope")
        sources = {item["event_id"]: item for item in batch}
        accepted = []
        for item in payload["candidates"]:
            if not isinstance(item, dict) or set(item) != {
                "predicate",
                "value",
                "quote",
                "source_event_id",
                "confidence",
                "importance",
            }:
                continue
            pred, value, quote, eid = (
                item[k] for k in ("predicate", "value", "quote", "source_event_id")
            )
            if (
                not isinstance(pred, str)
                or pred not in PREDICATES
                or not all(isinstance(v, str) for v in (value, quote, eid))
                or not 1 <= len(value) <= 160
                or not 1 <= len(quote) <= 300
                or eid not in sources
            ):
                continue
            negative = re.search(r"不喜欢|不爱|讨厌|不喝|不想", quote)
            if (pred == "likes" and negative) or (pred == "dislikes" and not negative):
                continue
            source = sources[eid]["text"]
            available = self.db.execute(
                "SELECT body FROM ltm_journal WHERE scope=? AND event_id=? AND role='user'",
                (self.scope, eid),
            ).fetchone()
            if (
                not available
                or not available[0]
                or quote not in source
                or quote not in available[0]
                or value not in quote
                or "我" not in quote
                or NONFACT.search(source)
                or safe_text(source) is None
            ):
                continue
            confidence, importance = item["confidence"], item["importance"]
            if (
                any(
                    isinstance(x, bool)
                    or not isinstance(x, (int, float))
                    or not math.isfinite(x)
                    for x in (confidence, importance)
                )
                or not 0.85 <= confidence <= 1
                or not 0 <= importance <= 1
            ):
                continue
            accepted.append((pred, value, quote, eid, confidence, importance))
        with self.db:
            if epoch != self.controls()["epoch"]:
                return 0
            for pred, value, quote, eid, confidence, importance in accepted:
                duplicate = self.db.execute(
                    "SELECT id,strength,anchor,half_life FROM ltm_items WHERE scope=? AND predicate=? AND value=? AND status='active'",
                    (self.scope, pred, value),
                ).fetchone()
                if duplicate:
                    mid = duplicate[0]
                    exists = self.db.execute(
                        "SELECT 1 FROM ltm_sources WHERE scope=? AND memory_id=? AND event_id=?",
                        (self.scope, mid, eid),
                    ).fetchone()
                    if not exists:
                        strengthened = min(1, strength_at(*duplicate[1:], now) + 0.15)
                        self.db.execute(
                            "UPDATE ltm_items SET strength=?,anchor=? WHERE scope=? AND id=?",
                            (strengthened, now, self.scope, mid),
                        )
                else:
                    if pred in {"nickname", "reply_style"}:
                        self.db.execute(
                            "UPDATE ltm_items SET status='superseded' WHERE scope=? AND predicate=? AND status IN ('active','frozen')",
                            (self.scope, pred),
                        )
                    if pred in {"likes", "dislikes"}:
                        opposite = "likes" if pred == "dislikes" else "dislikes"
                        self.db.execute(
                            "UPDATE ltm_items SET status='superseded' WHERE scope=? AND predicate=? AND value=? AND status IN ('active','frozen')",
                            (self.scope, opposite, value),
                        )
                    half_life = (
                        180
                        if pred in {"nickname", "reply_style", "likes", "dislikes"}
                        else (3 if pred == "project" else 14)
                    )
                    ttl = 360 if half_life == 180 else (30 if pred == "project" else 90)
                    mid = self.db.execute(
                        "INSERT INTO ltm_items(scope,predicate,value,quote,confidence,importance,strength,anchor,half_life,created,expires,status) VALUES(?,?,?,?,?,?,1,?,?,?,?, 'active')",
                        (
                            self.scope,
                            pred,
                            value,
                            quote,
                            confidence,
                            importance,
                            now,
                            half_life,
                            now,
                            now + ttl * DAY,
                        ),
                    ).lastrowid
                self.db.execute(
                    "INSERT OR IGNORE INTO ltm_sources VALUES(?,?,?)",
                    (self.scope, mid, eid),
                )
        return len(accepted)

    def list_memories(self, now=None, *, include_frozen=False):
        now = time.time() if now is None else now
        rows = self.db.execute(
            "SELECT id,predicate,value,quote,confidence,strength,anchor,half_life,expires FROM ltm_items WHERE scope=? AND status IN ('active'"
            + (",'frozen'" if include_frozen else "")
            + ") AND expires>? ORDER BY id",
            (self.scope, now),
        ).fetchall()
        return [
            {
                "id": f"A{mid}",
                "predicate": pred,
                "value": value,
                "quote": quote,
                "confidence": confidence,
                "strength": strength_at(strength, anchor, half_life, now),
            }
            for mid, pred, value, quote, confidence, strength, anchor, half_life, expires in rows
        ]

    def recall(self, query, now=None):
        def grams(text):
            text = re.sub(r"\s+", "", text)
            return {text[i : i + 2] for i in range(len(text) - 1)} - {
                "什么",
                "记得",
                "现在",
                "可以",
                "一下",
                "我的",
                "喜欢",
                "这个",
                "今天",
            }

        query_tokens = grams(query)
        historical = bool(re.search(r"以前|历史|从前|曾经", query))
        ranked = []
        for item in self.list_memories(now, include_frozen=historical):
            if item["strength"] < 0.35 and not historical:
                continue
            score = len(query_tokens & grams(item["value"] + item["quote"]))
            if score or item["predicate"] in {"nickname", "reply_style"}:
                ranked.append((score + item["strength"], item))
        return [
            item
            for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:6]
        ]

    def source(self, memory_id):
        row = self.db.execute(
            "SELECT predicate,quote,status FROM ltm_items WHERE scope=? AND id=?",
            (self.scope, memory_id),
        ).fetchone()
        if not row or row[2] == "deleted":
            return None
        origins = self.db.execute(
            "SELECT j.event_id,j.created,j.body IS NOT NULL FROM ltm_sources s LEFT JOIN ltm_journal j ON j.scope=s.scope AND j.event_id=s.event_id AND j.role='user' WHERE s.scope=? AND s.memory_id=?",
            (self.scope, memory_id),
        ).fetchall()
        return {
            "predicate": row[0],
            "quote": row[1],
            "status": row[2],
            "sources": [
                {"event_id": eid, "time": stamp, "original_available": bool(available)}
                for eid, stamp, available in origins
            ],
        }

    def delete(self, memory_id=None, *, pause=None, clear_all=False, _transaction=True):
        now = time.time()
        pause = self.controls()["paused"] if pause is None else pause
        with self.db if _transaction else nullcontext():
            self.db.execute(
                "UPDATE ltm_controls SET epoch=epoch+1,paused=? WHERE scope=?",
                (int(pause), self.scope),
            )
            epoch = self.controls()["epoch"]
            ids = [
                row[0]
                for row in self.db.execute(
                    "SELECT id FROM ltm_items WHERE scope=? AND status!='deleted'"
                    + ("" if clear_all else " AND id=?"),
                    (self.scope,) if clear_all else (self.scope, memory_id),
                )
            ]
            for mid in ids:
                sources = [
                    row[0]
                    for row in self.db.execute(
                        "SELECT event_id FROM ltm_sources WHERE scope=? AND memory_id=?",
                        (self.scope, mid),
                    )
                ]
                affected = {mid}
                for eid in sources:
                    affected.update(
                        row[0]
                        for row in self.db.execute(
                            "SELECT memory_id FROM ltm_sources WHERE scope=? AND event_id=?",
                            (self.scope, eid),
                        )
                    )
                    self.db.execute(
                        "UPDATE ltm_journal SET body=NULL WHERE scope=? AND event_id=?",
                        (self.scope, eid),
                    )
                for other in affected:
                    self.db.execute(
                        "INSERT OR REPLACE INTO ltm_tombstones VALUES(?,?,?,?)",
                        (self.scope, other, epoch, now),
                    )
                    self.db.execute(
                        "UPDATE ltm_items SET status='deleted',value='',quote='' WHERE scope=? AND id=?",
                        (self.scope, other),
                    )
            if clear_all:
                self.db.execute(
                    "UPDATE ltm_journal SET body=NULL WHERE scope=?", (self.scope,)
                )
                self.db.execute("DELETE FROM memories WHERE scope=?", (self.scope,))
            self.db.execute("DELETE FROM context WHERE scope=?", (self.scope,))
            self.db.execute("DELETE FROM ltm_affect WHERE scope=?", (self.scope,))
            self.db.execute(
                "UPDATE ltm_jobs SET state='invalidated' WHERE scope=? AND state='running'",
                (self.scope,),
            )
        return len(ids)

    def correct(self, memory_id, text):
        if (
            self.controls()["paused"]
            or not 1 <= len(text) <= 500
            or safe_text(text) is None
        ):
            raise ValueError("Correction is paused or invalid")
        with self.db:
            if self.source(memory_id) is None:
                return None
            if (
                self.db.execute(
                    "SELECT COUNT(*) FROM memories WHERE scope=?", (self.scope,)
                ).fetchone()[0]
                >= 30
            ):
                raise ValueError("Explicit memory limit reached")
            self.delete(memory_id, _transaction=False)
            new_id = self.db.execute(
                "INSERT INTO memories(scope,text) VALUES(?,?)", (self.scope, text)
            ).lastrowid
            self.db.execute(
                "INSERT OR REPLACE INTO ltm_corrections VALUES(?,?,?,?)",
                (self.scope, memory_id, new_id, time.time()),
            )
        return new_id

    def forget_related(self, text):
        for item in self.list_memories(include_frozen=True):
            if item["value"] in text or item["quote"] in text:
                self.delete(int(item["id"][1:]))
        with self.db:
            self.db.execute(
                "UPDATE ltm_journal SET body=NULL WHERE scope=? AND instr(body,?)>0",
                (self.scope, text),
            )
        self.delete()  # Invalidate old extraction snapshots and recent context.

    def pause(self, paused):
        with self.db:
            self.db.execute(
                "UPDATE ltm_controls SET epoch=epoch+1,paused=? WHERE scope=?",
                (int(paused), self.scope),
            )
            self.db.execute("DELETE FROM context WHERE scope=?", (self.scope,))

    def expire(self, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute(
                "UPDATE ltm_journal SET body=NULL WHERE scope=? AND created<?",
                (self.scope, now - 7 * DAY),
            )
            self.db.execute(
                "UPDATE ltm_items SET status='expired',value='',quote='' WHERE scope=? AND status!='deleted' AND expires<=?",
                (self.scope, now),
            )
            for item in self.list_memories(now):
                if item["strength"] < 0.15:
                    self.db.execute(
                        "UPDATE ltm_items SET status='frozen' WHERE scope=? AND id=?",
                        (self.scope, int(item["id"][1:])),
                    )
