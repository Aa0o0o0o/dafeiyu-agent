"""Scoped authority: all public operations verify SQL scope before reading text."""

import json
import re
from uuid import uuid4

from .repository import Repository
from .decay import Clock, DAY, POLICIES, reinforced, state_at, strength
from .admission import digest, subject_id, safe_source, candidate
from .retrieval import RetrievalMixin
from .deletion import DeletionMixin
from .maintenance import MaintenanceMixin
from .graph import GraphMixin


class MemoryService(RetrievalMixin, DeletionMixin, MaintenanceMixin, GraphMixin):
    def __init__(
        self,
        db,
        scope,
        bot_id,
        instance_id,
        owner_id,
        *,
        clock=None,
        options=None,
        registry=None,
    ):
        self.repo = Repository(db)
        self.db, self.scope, self.bot_id, self.owner_id = db, scope, bot_id, owner_id
        self.clock, self.options, self.registry = (
            clock or Clock(),
            options or {},
            registry,
        )
        with self.repo.transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO mv2_scopes(scope_id,bot_id,platform_instance_id,created) VALUES(?,?,?,?)",
                (scope, bot_id, instance_id, self.now()),
            )

    def now(self):
        return self.clock.now()

    def control(self):
        return self.repo.one("SELECT * FROM mv2_scopes WHERE scope_id=?", (self.scope,))

    def policy(self, subject):
        row = self.repo.one(
            "SELECT * FROM mv2_subject_policies WHERE scope_id=? AND subject_id=?",
            (self.scope, subject),
        )
        return row or {
            "scope_id": self.scope,
            "subject_id": subject,
            "enabled": 1,
            "epoch": 0,
        }

    def permitted(self, subjects):
        return bool(self.control()["enabled"]) and all(
            self.policy(s)["enabled"] for s in subjects
        )

    def snapshot(self):
        c = self.control()
        return {
            "scope_id": self.scope,
            "scope_epoch": c["epoch"],
            "policy_version": c["policy_version"],
        }

    def unchanged(self, snapshot):
        return snapshot == self.snapshot()

    def get(self, mid):
        return self.repo.one(
            "SELECT * FROM mv2_memories WHERE scope_id=? AND id=?", (self.scope, mid)
        )

    def source(self, sid):
        source = self.repo.one(
            "SELECT * FROM mv2_sources WHERE scope_id=? AND id=?", (self.scope, sid)
        )
        if source:
            source["subjects"] = [
                r[0]
                for r in self.db.execute(
                    "SELECT subject_id FROM mv2_source_subjects WHERE source_id=?",
                    (sid,),
                )
            ]
            if source["expires_at"] <= self.now():
                source["body"], source["source_state"] = None, "expired"
        return source

    def register_alias(self, actor, alias):
        if (
            not subject_id(actor)
            or not isinstance(alias, str)
            or not 1 <= len(alias) <= 50
            or not safe_source(alias)
        ):
            return
        with self.repo.transaction():
            if self.policy(actor)["enabled"]:
                self.db.execute(
                    "INSERT OR IGNORE INTO mv2_aliases VALUES(?,?,?)",
                    (self.scope, actor, alias),
                )
            else:
                self.db.execute(
                    "INSERT OR IGNORE INTO mv2_blocked_aliases VALUES(?,?,?,?)",
                    (self.scope, actor, len(alias), digest(alias)),
                )

    def observe(self, event):
        sender, text, eid = (
            event.get("sender_id"),
            event.get("text"),
            event.get("event_id"),
        )
        subjects = set(event.get("subject_ids", [])) | {sender}
        now = self.now()
        occurred = event.get("event_time", now)
        if (
            not subject_id(sender)
            or not isinstance(eid, str)
            or not 1 <= len(eid) <= 100
            or not all(subject_id(s) for s in subjects)
            or event.get("source_kind", "user") != "user"
            or sender == self.bot_id
            or event.get("quoted")
            or event.get("has_media")
            or not isinstance(occurred, (int, float))
            or isinstance(occurred, bool)
            or not now - 7 * DAY < occurred <= now + 300
            or not safe_source(text)
            or not self.permitted(subjects)
        ):
            return None
        # A known opted-out alias cannot be kept merely because an @ was omitted.
        blocked = self.repo.rows(
            "SELECT a.alias FROM mv2_aliases a JOIN mv2_subject_policies p ON p.scope_id=a.scope_id AND p.subject_id=a.entity_id WHERE a.scope_id=? AND p.enabled=0",
            (self.scope,),
        )
        if any(r["alias"] and r["alias"] in text for r in blocked):
            return None
        blocked_hashes = self.repo.rows(
            "SELECT length,alias_hash FROM mv2_blocked_aliases WHERE scope_id=?",
            (self.scope,),
        )
        for item in blocked_hashes:
            length = item["length"]
            if any(
                digest(text[i : i + length]) == item["alias_hash"]
                for i in range(max(0, len(text) - length + 1))
            ):
                return None
        if self.db.execute(
            "SELECT 1 FROM mv2_suppression WHERE scope_id=? AND event_id=?",
            (self.scope, eid),
        ).fetchone():
            return None
        sid = "E" + digest(self.scope, eid, 1)[:32]
        occurred = min(now, occurred)
        with self.repo.transaction():
            if not self.permitted(subjects):
                return None
            self.db.execute(
                "INSERT OR IGNORE INTO mv2_sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (
                    sid,
                    self.scope,
                    eid,
                    1,
                    sender,
                    text,
                    occurred,
                    now,
                    occurred + 7 * DAY,
                    "available",
                    "user",
                    str(int(occurred // 1800)),
                ),
            )
            for subject in subjects:
                self.db.execute(
                    "INSERT OR IGNORE INTO mv2_source_subjects VALUES(?,?)",
                    (sid, subject),
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO mv2_entities VALUES(?,?,'person')",
                    (self.scope, subject),
                )
            alias = event.get("display_name")
            if isinstance(alias, str) and 1 <= len(alias) <= 50 and safe_source(alias):
                self.db.execute(
                    "INSERT OR IGNORE INTO mv2_aliases VALUES(?,?,?)",
                    (self.scope, sender, alias),
                )
        return sid

    def pending(self, limit=30, day=None):
        rows = self.repo.rows(
            "SELECT id FROM mv2_sources WHERE scope_id=? AND processed=0 AND source_state='available' AND body IS NOT NULL AND expires_at>? ORDER BY event_time LIMIT ?",
            (self.scope, self.now(), limit),
        )
        sources = [self.source(r["id"]) for r in rows]
        return [
            s
            for s in sources
            if self.permitted(s["subjects"])
            and (day is None or self.business_day(s["event_time"]) == day)
        ]

    def apply(self, payload, expected_epochs, *, allowed_source_ids=None, lease=None):
        if (
            not isinstance(payload, dict)
            or set(payload) != {"candidates"}
            or not isinstance(payload["candidates"], list)
            or len(payload["candidates"]) > 30
        ):
            raise ValueError("Invalid candidate envelope")
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 30000:
            raise ValueError("Candidate envelope too large")
        with self.repo.transaction():
            if not self.unchanged(expected_epochs):
                return {"accepted": 0, "reason": "stale_epoch"}
            if lease and not self.lease_valid(lease):
                return {"accepted": 0, "reason": "stale_lease"}
            output_hash = digest(payload)
            if self.db.execute(
                "SELECT 1 FROM mv2_proposals WHERE scope_id=? AND extractor_version=? AND output_hash=?",
                (self.scope, "2.1", output_hash),
            ).fetchone():
                return {"accepted": 0, "ids": [], "reason": "already_applied"}
            sources = {}
            for item in payload["candidates"]:
                ids = item.get("source_event_ids", []) if isinstance(item, dict) else []
                if not isinstance(ids, list):
                    continue
                for sid in ids:
                    if isinstance(sid, str) and (
                        allowed_source_ids is None or sid in allowed_source_ids
                    ):
                        s = self.source(sid)
                        if s and self.permitted(s["subjects"]):
                            sources[sid] = s
            accepted = []
            for item in payload["candidates"]:
                try:
                    item = candidate(item, sources, self.now())
                except (TypeError, ValueError, KeyError):
                    item = None
                if item:
                    accepted.append(self._apply_candidate(item, sources))
            if any(accepted):
                self.db.execute(
                    "INSERT OR IGNORE INTO mv2_proposals VALUES(?,?,?,?)",
                    (self.scope, "2.1", output_hash, self.now()),
                )
            return {
                "accepted": len([x for x in accepted if x]),
                "ids": [x for x in accepted if x],
            }

    def _apply_candidate(self, item, sources):
        subject, predicate = item["subject_ids"][0], item["predicate"]
        temporal = item["temporal_class"]
        if temporal == "temporary_state":
            predicate = "temporary:" + predicate
        selected = [sources[sid] for sid in item["source_event_ids"]]
        latest_source = max(selected, key=lambda source: source["event_time"])
        event_time = max(s["event_time"] for s in selected)
        # Derived copies and same-day repetition cannot manufacture reinforcement.
        group = digest(
            self.scope, subject, predicate, item["value"], latest_source["session_id"]
        )
        existing = self.repo.one(
            "SELECT * FROM mv2_memories WHERE scope_id=? AND subject_id=? AND predicate=? AND value=? AND polarity=? AND status IN ('candidate','active','dormant','frozen') ORDER BY created_at DESC LIMIT 1",
            (self.scope, subject, predicate, item["value"], item["polarity"]),
        )
        if existing and state_at(existing, self.now()) == "expired":
            existing = None
        reconfirmed = item["assertion_mode"] == "self_report" and any(
            re.search(r"现在仍然|现在还是|依然喜欢|确认.{0,8}仍然", s["body"])
            for s in selected
        )
        if existing and (
            reconfirmed
            or (
                item["assertion_mode"] == "self_report"
                and existing["assertion_mode"] != "self_report"
            )
        ):
            self.db.execute(
                "UPDATE mv2_memories SET status='superseded',valid_to=?,superseded_at=?,revision=revision+1 WHERE id=?",
                (event_time, self.now(), existing["id"]),
            )
            self._changed(existing["id"], "reconfirmed_version")
            existing = None
        if existing:
            mid = existing["id"]
            duplicate = self.db.execute(
                "SELECT 1 FROM mv2_evidence WHERE memory_id=? AND evidence_group_id=?",
                (mid, group),
            ).fetchone()
            if not duplicate and event_time >= existing["last_evidence_at"] + DAY:
                s, h = reinforced(existing, event_time)
                self.db.execute(
                    "UPDATE mv2_memories SET revision=revision+1,strength_at_anchor=?,anchor_at=?,half_life_days=?,last_evidence_at=?,independent_evidence_count=independent_evidence_count+1,updated_at=? WHERE id=?",
                    (s, event_time, h, event_time, self.now(), mid),
                )
                if temporal == "stable_preference":
                    self.db.execute(
                        "UPDATE mv2_memories SET review_at=? WHERE id=?",
                        (event_time + 360 * DAY, mid),
                    )
        else:
            mid = self._new_memory(item, predicate, event_time)
            past = any(
                re.search(r"以前|过去|曾经", s["body"][: s["body"].find(item["value"])])
                and ("现在" not in s["body"][: s["body"].find(item["value"])])
                for s in selected
                if item["value"] in s["body"]
            )
            if past:
                self.db.execute(
                    "UPDATE mv2_memories SET status='superseded',valid_from=NULL,valid_to=?,event_time_start=NULL,event_time_end=?,time_precision='unknown' WHERE id=?",
                    (event_time, event_time, mid),
                )
            if temporal == "temporary_state":
                from datetime import datetime, timedelta
                from .maintenance import TZ

                expiry = event_time + 3 * DAY
                if any("今天" in s["body"] for s in selected):
                    local = datetime.fromtimestamp(event_time, TZ)
                    expiry = (
                        local.replace(hour=0, minute=0, second=0, microsecond=0)
                        + timedelta(days=1)
                    ).timestamp()
                self.db.execute(
                    "UPDATE mv2_memories SET expires_at=? WHERE id=?", (expiry, mid)
                )
            if item["assertion_mode"] == "self_report" and temporal not in {
                "style",
                "episode",
                "important_episode",
            }:
                # Multi-valued likes aren't an exclusive slot; negation is exclusive.
                old_rows = self.repo.rows(
                    "SELECT * FROM mv2_memories WHERE scope_id=? AND subject_id=? AND id!=? AND status IN ('active','dormant','frozen') AND (predicate=? OR (?='likes' AND predicate='dislikes' AND value=?) OR (?='dislikes' AND predicate='likes' AND value=?))",
                    (
                        self.scope,
                        subject,
                        mid,
                        predicate,
                        predicate,
                        item["value"],
                        predicate,
                        item["value"],
                    ),
                )
                for old in old_rows:
                    if (
                        predicate in {"likes", "dislikes"}
                        and old["predicate"] == predicate
                        and old["value"] != item["value"]
                    ):
                        continue
                    self.db.execute(
                        "UPDATE mv2_memories SET status='superseded',valid_to=?,superseded_at=?,revision=revision+1 WHERE id=?",
                        (event_time, self.now(), old["id"]),
                    )
                    self.db.execute(
                        "INSERT OR IGNORE INTO mv2_dependencies VALUES(?,?,'supersedes')",
                        (old["id"], mid),
                    )
                    self._changed(old["id"], "superseded")
        for source in selected:
            source_group = digest(
                self.scope, subject, predicate, item["value"], source["session_id"]
            )
            self.db.execute(
                "INSERT OR IGNORE INTO mv2_evidence VALUES(?,?,?,?,?)",
                (mid, source["id"], source_group, "support", self.now()),
            )
            for involved in source["subjects"]:
                self.db.execute(
                    "INSERT OR IGNORE INTO mv2_memory_subjects VALUES(?,?,'involved')",
                    (mid, involved),
                )
        self.db.execute(
            "UPDATE mv2_memories SET independent_evidence_count=(SELECT COUNT(DISTINCT evidence_group_id) FROM mv2_evidence WHERE memory_id=?) WHERE id=?",
            (mid, mid),
        )
        current = self.get(mid)
        if current["status"] == "candidate":
            sessions = self.db.execute(
                "SELECT COUNT(DISTINCT s.session_id) FROM mv2_evidence e JOIN mv2_sources s ON s.id=e.source_id WHERE e.memory_id=?",
                (mid,),
            ).fetchone()[0]
            can_activate = True
            if item["kind"] == "style":
                active_count = self.db.execute(
                    "SELECT COUNT(*) FROM mv2_memories WHERE scope_id=? AND kind='style' AND status='active'",
                    (self.scope,),
                ).fetchone()[0]
                today_count = self.db.execute(
                    "SELECT COUNT(*) FROM mv2_style_activations WHERE scope_id=? AND business_date=?",
                    (self.scope, self.business_day()),
                ).fetchone()[0]
                can_activate = active_count < 50 and today_count < 3
            if (
                sessions >= 3
                and item["assertion_mode"] != "third_party"
                and can_activate
            ):
                self.db.execute(
                    "UPDATE mv2_memories SET status='active',revision=revision+1 WHERE id=?",
                    (mid,),
                )
                if item["kind"] == "style":
                    self.db.execute(
                        "INSERT OR IGNORE INTO mv2_style_activations VALUES(?,?,?)",
                        (self.scope, mid, self.business_day()),
                    )
        self._changed(mid, "upsert")
        summary = item.get("summary")
        if (
            isinstance(summary, str)
            and 2 <= len(summary) <= 60
            and summary in item["value"]
        ):
            start = item["value"].find(summary)
            self.db.execute(
                "INSERT OR REPLACE INTO mv2_gist_spans VALUES(?,?,?)",
                (mid, start, start + len(summary)),
            )
        self.build_edges(mid, source_id=latest_source["id"])
        return mid

    def _new_memory(self, item, predicate, event_time, *, protected=False):
        now = self.now()
        temporal = "core" if protected else item["temporal_class"]
        half, ttl, _ = POLICIES[temporal]
        status = (
            "active"
            if item["assertion_mode"] in {"self_report", "verified_action"}
            and temporal != "style"
            else "candidate"
        )
        if not protected:
            active = self.db.execute(
                "SELECT COUNT(*) FROM mv2_memories WHERE scope_id=? AND protected=0 AND status IN ('active','candidate','dormant')",
                (self.scope,),
            ).fetchone()[0]
            cold = self.db.execute(
                "SELECT COUNT(*) FROM mv2_memories WHERE scope_id=? AND status IN ('frozen','superseded') AND value!=''",
                (self.scope,),
            ).fetchone()[0]
            if active >= self.options.get(
                "active_capacity", 3000
            ) or cold >= self.options.get("cold_capacity", 20000):
                raise ValueError("Memory capacity reached; cleanup/review required")
        mid = "V" + uuid4().hex[:20]
        layer = {
            "core": "M0",
            "working_summary": "M1",
            "episode": "M2",
            "important_episode": "M2",
            "stable_preference": "M3",
            "temporary_state": "M3",
            "style": "M4",
        }[temporal]
        c = self.control()
        row = dict(
            id=mid,
            scope_id=self.scope,
            revision=1,
            kind="core" if protected else item["kind"],
            layer=layer,
            status=status,
            subject_id=item["subject_ids"][0],
            predicate=predicate,
            value=item["value"],
            summary=f"{item['subject_ids'][0]}：{item['value']}",
            polarity=item.get("polarity", "positive"),
            assertion_mode=item["assertion_mode"],
            confidence_score=item["confidence_score"],
            confidence_basis="grounded_source",
            importance=item.get("importance", 0.5),
            temporal_class=temporal,
            event_time_start=event_time,
            event_time_end=None,
            time_precision="interval",
            valid_from=event_time,
            valid_to=None,
            recorded_at=now,
            superseded_at=None,
            last_evidence_at=event_time,
            independent_evidence_count=1,
            strength_at_anchor=1,
            anchor_at=event_time,
            half_life_days=half,
            base_half_life_days=half,
            last_used_at=None,
            protected=int(protected),
            expires_at=None
            if temporal in {"core", "stable_preference", "important_episode"}
            else event_time + ttl * DAY,
            review_at=event_time + ttl * DAY
            if temporal in {"stable_preference", "important_episode"}
            else None,
            scope_epoch=c["epoch"],
            policy_version=c["policy_version"],
            created_at=now,
            updated_at=now,
        )
        self.repo.insert("mv2_memories", row)
        if status == "candidate" and row["expires_at"] is None:
            self.db.execute(
                "UPDATE mv2_memories SET expires_at=? WHERE id=?",
                (event_time + 30 * DAY, mid),
            )
        for subject in item["subject_ids"]:
            self.db.execute(
                "INSERT OR IGNORE INTO mv2_memory_subjects VALUES(?,?,'subject')",
                (mid, subject),
            )
        return mid

    def _changed(self, mid, kind):
        row = self.get(mid)
        if row:
            self.repo.event(self.scope, mid, row["revision"], kind, self.now())
            self.repo.index(row)

    def remember(self, actor, value, eid, *, protected=True):
        if not safe_source(value) or not self.permitted([actor]):
            raise ValueError("Memory disabled or unsafe")
        sid = self.observe(
            {
                "event_id": eid,
                "sender_id": actor,
                "subject_ids": [actor],
                "text": "我明确要求记住：" + value,
                "event_time": self.now(),
            }
        )
        if not sid:
            raise ValueError("Source rejected")
        item = {
            "kind": "semantic",
            "subject_ids": [actor],
            "predicate": "explicit",
            "value": value,
            "assertion_mode": "self_report",
            "confidence_score": 1,
            "importance": 0.7,
            "temporal_class": "stable_preference",
        }
        with self.repo.transaction():
            mid = self._new_memory(item, "explicit", self.now(), protected=protected)
            self.db.execute(
                "INSERT INTO mv2_evidence VALUES(?,?,?,?,?)",
                (mid, sid, digest(sid), "support", self.now()),
            )
            self.db.execute("UPDATE mv2_sources SET processed=1 WHERE id=?", (sid,))
            self._changed(mid, "remember")
        return mid

    def propose(self, batch):
        # Transport-neutral provider contract; source IDs never come from the LLM.
        return [
            {
                "id": source["id"],
                "subject_ids": source["subjects"],
                "sender_id": source["sender_id"],
                "text": source["body"],
            }
            for source in batch
            if source["scope_id"] == self.scope
            and source["body"]
            and source["expires_at"] > self.now()
            and self.permitted(source["subjects"])
        ]

    def register_pending_action(self, actor, summary, receipt):
        if not receipt or not safe_source(summary) or not self.permitted([actor]):
            raise ValueError("Task requires an actual authorized creation receipt")
        mid = "K" + digest(self.scope, str(receipt))[:20]
        with self.repo.transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO mv2_pending_actions VALUES(?,?,?,'pending',?,?,?)",
                (mid, self.scope, actor, summary, self.now(), digest(str(receipt))),
            )
        return mid

    def list_memories(self, actor, *, review=False):
        return [
            self.project(row)
            for row in self.repo.rows(
                "SELECT m.* FROM mv2_memories m JOIN mv2_memory_subjects s ON s.memory_id=m.id WHERE m.scope_id=? AND s.subject_id=? AND m.status!=? ORDER BY m.created_at DESC LIMIT 100",
                (self.scope, actor, "retracted"),
            )
            if self.permitted([actor])
            and state_at(row, self.now()) != "expired"
            and (not review or row["status"] == "candidate")
        ]

    def project(self, row, at=None):
        at = self.now() if at is None else at
        result = dict(row)
        result["strength_now"] = strength(row, at)
        result["status_now"] = state_at(row, at)
        result["origin_layer"] = row["layer"]
        if result["status_now"] in {"frozen", "superseded"}:
            result["layer"] = "M5"
        result["subjects"] = [
            r[0]
            for r in self.db.execute(
                "SELECT subject_id FROM mv2_memory_subjects WHERE memory_id=?",
                (row["id"],),
            )
        ]
        result["source_state"] = (
            "available"
            if self.db.execute(
                "SELECT 1 FROM mv2_evidence e JOIN mv2_sources s ON s.id=e.source_id WHERE e.memory_id=? AND s.scope_id=? AND s.body IS NOT NULL AND s.source_state='available' AND s.expires_at>?",
                (row["id"], self.scope, at),
            ).fetchone()
            else "expired"
        )
        return result

    def explain(self, actor, mid, *, admin=False):
        row = self.get(mid)
        if (
            not row
            or row["status"] == "retracted"
            or (not admin and actor not in self.project(row)["subjects"])
        ):
            return None
        result = self.project(row)
        result["evidence"] = []
        for ev in self.repo.rows(
            "SELECT * FROM mv2_evidence WHERE memory_id=?", (mid,)
        ):
            source = self.source(ev["source_id"])
            if source:
                result["evidence"].append(
                    {
                        "source_id": source["id"],
                        "sender_id": source["sender_id"],
                        "source_state": source["source_state"],
                        "event_time": source["event_time"],
                        "excerpt": source["body"][:300]
                        if source["body"] and self.permitted(source["subjects"])
                        else None,
                    }
                )
        result["dependencies"] = self.repo.rows(
            "SELECT parent_id,child_id,relation_type FROM mv2_dependencies WHERE parent_id=? OR child_id=?",
            (mid, mid),
        )
        result["curve"] = [
            {
                "days": d,
                "strength": strength(row, self.now() + d * DAY),
                "state": state_at(row, self.now() + d * DAY),
            }
            for d in [0, 1, 7, 14, 30, 60, 90, 120, 180]
        ]
        return result
