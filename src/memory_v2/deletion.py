"""Epoch invalidation, transitive erasure and body-free restore suppression."""

import json
import os
from pathlib import Path
from filelock import FileLock

from .admission import safe_source, digest


class DeletionMixin:
    def _registry_append(self, record):
        if self.registry is None:
            return
        path = Path(self.registry)
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path) + ".lock"), path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())

    def _related(self, initial):
        affected, sources = set(initial), set()
        while True:
            before = len(affected), len(sources)
            for mid in list(affected):
                affected.update(
                    r[0]
                    for r in self.db.execute(
                        "SELECT child_id FROM mv2_dependencies WHERE parent_id=? AND relation_type!='supersedes'",
                        (mid,),
                    )
                )
                sources.update(
                    r[0]
                    for r in self.db.execute(
                        "SELECT source_id FROM mv2_evidence WHERE memory_id=?", (mid,)
                    )
                )
            for source in list(sources):
                affected.update(
                    r[0]
                    for r in self.db.execute(
                        "SELECT e.memory_id FROM mv2_evidence e JOIN mv2_memories m ON m.id=e.memory_id WHERE e.source_id=? AND m.scope_id=?",
                        (source, self.scope),
                    )
                )
            if before == (len(affected), len(sources)):
                return affected, sources

    def _erase(self, affected, sources):
        for sid in sources:
            self.db.execute(
                "UPDATE mv2_sources SET body=NULL,source_state='retracted',processed=1 WHERE id=? AND scope_id=?",
                (sid, self.scope),
            )
        for mid in affected:
            row = self.get(mid)
            if not row:
                continue
            self.db.execute(
                "UPDATE mv2_memories SET value='',summary='',confidence_basis='',status='retracted',revision=revision+1,protected=0,updated_at=? WHERE scope_id=? AND id=?",
                (self.now(), self.scope, mid),
            )
            self.db.execute(
                "DELETE FROM mv2_fts WHERE scope_id=? AND memory_id=?",
                (self.scope, mid),
            )
            self.db.execute("DELETE FROM mv2_gist_spans WHERE parent_id=?", (mid,))
            self.db.execute(
                "DELETE FROM mv2_edges WHERE scope_id=? AND (src=? OR dst=?)",
                (self.scope, mid, mid),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO mv2_tombstones VALUES(?,?,?,?,?)",
                (self.scope, "memory", mid, self.control()["epoch"], self.now()),
            )
            self._changed(mid, "delete")
        # Drop traces/layouts instead of retaining a second path to erased material.
        self.db.execute("DELETE FROM mv2_uses WHERE scope_id=?", (self.scope,))
        self.db.execute("DELETE FROM mv2_layouts WHERE scope_id=?", (self.scope,))
        self.db.execute(
            "DELETE FROM context WHERE scope=?", (self.scope,)
        ) if self._has_table("context") else None
        self.db.execute(
            "DELETE FROM context_clock WHERE scope=?", (self.scope,)
        ) if self._has_table("context_clock") else None
        # Archived V1 rows are not a second readable copy after deletion.
        if self._has_table("memories"):
            self.db.execute("DELETE FROM memories WHERE scope=?", (self.scope,))
        if self._has_table("ltm_items"):
            self.db.execute(
                "UPDATE ltm_items SET value='',quote='',status='deleted' WHERE scope=?",
                (self.scope,),
            )
            self.db.execute(
                "UPDATE ltm_journal SET body=NULL WHERE scope=?", (self.scope,)
            )

    def _has_table(self, name):
        return (
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            is not None
        )

    def forget(self, actor, target=None, *, all_mine=False, off=False, admin=False):
        with self.repo.transaction():
            if target:
                row = self.get(target)
                if (
                    not row
                    or row["status"] == "retracted"
                    or (not admin and actor not in self.project(row)["subjects"])
                ):
                    return False
                initial = {target}
            else:
                initial = {
                    r[0]
                    for r in self.db.execute(
                        "SELECT m.id FROM mv2_memories m JOIN mv2_memory_subjects s ON s.memory_id=m.id WHERE m.scope_id=? AND s.subject_id=?",
                        (self.scope, actor),
                    )
                }
            affected, sources = self._related(initial)
            if all_mine or off:
                sources.update(
                    r[0]
                    for r in self.db.execute(
                        "SELECT s.id FROM mv2_sources s JOIN mv2_source_subjects p ON p.source_id=s.id WHERE s.scope_id=? AND p.subject_id=?",
                        (self.scope, actor),
                    )
                )
            event_ids = [
                r[0]
                for sid in sources
                for r in self.db.execute(
                    "SELECT event_id FROM mv2_sources WHERE scope_id=? AND id=?",
                    (self.scope, sid),
                )
            ]
            epoch = self.control()["epoch"] + 1
            record = {
                "scope_id": self.scope,
                "scope_epoch": epoch,
                "target_subject": actor if all_mine or off else None,
                "off": bool(off),
                "enabled": 0 if off else self.policy(actor)["enabled"],
                "memory_ids": sorted(affected),
                "source_ids": sorted(sources),
                "event_ids": sorted(event_ids),
                "created": self.now(),
                "operation": "forget",
            }
            if off:
                record["blocked_aliases"] = [
                    {
                        "length": len(alias["alias"]),
                        "alias_hash": digest(alias["alias"]),
                    }
                    for alias in self.repo.rows(
                        "SELECT alias FROM mv2_aliases WHERE scope_id=? AND entity_id=?",
                        (self.scope, actor),
                    )
                ]
            record["legacy_keys"] = [
                r[0]
                for mid in affected
                for r in self.db.execute(
                    "SELECT legacy_key FROM mv2_migrations WHERE scope_id=? AND new_id=?",
                    (self.scope, mid),
                )
            ]
            self._registry_append(
                record
            )  # Durable suppression precedes acknowledgment.
            self.replay_record(record)
            return True

    def replay_record(self, record):
        if record["scope_id"] != self.scope:
            return
        with self.repo.transaction():
            record_hash = digest(record)
            if self.db.execute(
                "SELECT 1 FROM mv2_registry_applied WHERE scope_id=? AND record_hash=?",
                (self.scope, record_hash),
            ).fetchone():
                return
            self.db.execute(
                "UPDATE mv2_scopes SET epoch=MAX(epoch,?),policy_version=policy_version+1 WHERE scope_id=?",
                (record["scope_epoch"], self.scope),
            )
            subject = record.get("target_subject")
            if subject:
                if record.get("off"):
                    for alias in record.get("blocked_aliases", []):
                        self.db.execute(
                            "INSERT OR IGNORE INTO mv2_blocked_aliases VALUES(?,?,?,?)",
                            (self.scope, subject, alias["length"], alias["alias_hash"]),
                        )
                    for alias in self.repo.rows(
                        "SELECT alias FROM mv2_aliases WHERE scope_id=? AND entity_id=?",
                        (self.scope, subject),
                    ):
                        self.db.execute(
                            "INSERT OR IGNORE INTO mv2_blocked_aliases VALUES(?,?,?,?)",
                            (
                                self.scope,
                                subject,
                                len(alias["alias"]),
                                digest(alias["alias"]),
                            ),
                        )
                self.db.execute(
                    "INSERT OR IGNORE INTO mv2_subject_policies(scope_id,subject_id) VALUES(?,?)",
                    (self.scope, subject),
                )
                self.db.execute(
                    "UPDATE mv2_subject_policies SET enabled=?,epoch=epoch+1 WHERE scope_id=? AND subject_id=?",
                    (
                        record.get("enabled", 0 if record.get("off") else 1),
                        self.scope,
                        subject,
                    ),
                )
            for eid in record.get("event_ids", []):
                self.db.execute(
                    "INSERT OR IGNORE INTO mv2_suppression VALUES(?,?)",
                    (self.scope, eid),
                )
            initial = set(record.get("memory_ids", []))
            for key in record.get("legacy_keys", []):
                initial.update(
                    r[0]
                    for r in self.db.execute(
                        "SELECT new_id FROM mv2_migrations WHERE scope_id=? AND legacy_key=?",
                        (self.scope, key),
                    )
                )
            for sid in record.get("source_ids", []):
                initial.update(
                    r[0]
                    for r in self.db.execute(
                        "SELECT e.memory_id FROM mv2_evidence e JOIN mv2_memories m ON m.id=e.memory_id WHERE e.source_id=? AND m.scope_id=?",
                        (sid, self.scope),
                    )
                )
            if subject and record.get("operation") != "on":
                initial.update(
                    r[0]
                    for r in self.db.execute(
                        "SELECT m.id FROM mv2_memories m JOIN mv2_memory_subjects p ON p.memory_id=m.id WHERE m.scope_id=? AND p.subject_id=?",
                        (self.scope, subject),
                    )
                )
            # A restored database can have additional derived nodes; recompute closure.
            affected, sources = self._related(initial)
            sources.update(record.get("source_ids", []))
            if subject and record.get("operation") != "on":
                sources.update(
                    r[0]
                    for r in self.db.execute(
                        "SELECT s.id FROM mv2_sources s JOIN mv2_source_subjects p ON p.source_id=s.id WHERE s.scope_id=? AND p.subject_id=?",
                        (self.scope, subject),
                    )
                )
            if record.get("operation") != "on":
                self._erase(affected, sources)
                if subject:
                    self.db.execute(
                        "DELETE FROM mv2_aliases WHERE scope_id=? AND entity_id=?",
                        (self.scope, subject),
                    )
                    self.db.execute(
                        "UPDATE mv2_pending_actions SET summary='',state='cancelled_by_subject' WHERE scope_id=? AND subject_id=?",
                        (self.scope, subject),
                    )
            if record.get("operation") == "on":
                self.db.execute(
                    "UPDATE mv2_subject_policies SET enabled=1 WHERE scope_id=? AND subject_id=?",
                    (self.scope, subject),
                )
                self.db.execute(
                    "DELETE FROM mv2_blocked_aliases WHERE scope_id=? AND subject_id=?",
                    (self.scope, subject),
                )
            self.repo.event(self.scope, None, None, "policy", self.now())
            self.db.execute(
                "INSERT OR IGNORE INTO mv2_registry_applied VALUES(?,?)",
                (self.scope, record_hash),
            )

    def replay_registry(self):
        if self.registry and Path(self.registry).exists():
            with FileLock(str(self.registry) + ".lock"):
                records = [
                    json.loads(line)
                    for line in Path(self.registry)
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if line
                ]
            for record in records:
                self.replay_record(record)

    def on_me(self, actor):
        record = {
            "scope_id": self.scope,
            "scope_epoch": self.control()["epoch"] + 1,
            "target_subject": actor,
            "off": False,
            "memory_ids": [],
            "source_ids": [],
            "event_ids": [],
            "created": self.now(),
            "operation": "on",
        }
        self._registry_append(record)
        # Enabling starts from new events; previously erased bodies remain erased.
        self.replay_record(record)

    def correct(self, actor, mid, value, eid):
        row = self.get(mid)
        from .decay import state_at

        if (
            not row
            or row["subject_id"] != actor
            or not safe_source(value)
            or state_at(row, self.now()) in {"expired", "retracted"}
        ):
            return None
        with self.repo.transaction():
            source = self.observe(
                {
                    "event_id": eid,
                    "sender_id": actor,
                    "subject_ids": [actor],
                    "text": "我明确纠正：" + value,
                    "event_time": self.now(),
                }
            )
            if not source:
                return None
            # Preserve the old value only as legitimate dated history for a change,
            # explicit corrections retract it and every dependent claim instead.
            self.forget(actor, mid)
            item = {
                "kind": row["kind"] if row["kind"] != "core" else "semantic",
                "subject_ids": [actor],
                "value": value,
                "assertion_mode": "self_report",
                "confidence_score": 1,
                "importance": row["importance"],
                "temporal_class": row["temporal_class"]
                if row["temporal_class"] != "core"
                else "stable_preference",
            }
            event_time = (
                row["event_time_start"] or self.now()
                if row["kind"] == "episode"
                else self.now()
            )
            new_id = self._new_memory(
                item, row["predicate"], event_time, protected=bool(row["protected"])
            )
            if row["kind"] == "episode":
                self.db.execute(
                    "UPDATE mv2_memories SET expires_at=? WHERE id=?",
                    (row["expires_at"], new_id),
                )
            self.db.execute(
                "INSERT INTO mv2_evidence VALUES(?,?,?,?,?)",
                (new_id, source, digest(source), "support", self.now()),
            )
            self.db.execute("UPDATE mv2_sources SET processed=1 WHERE id=?", (source,))
            self._changed(new_id, "correction")
            self.db.execute(
                "INSERT OR IGNORE INTO mv2_dependencies VALUES(?,?,'supersedes')",
                (mid, new_id),
            )
            return new_id

    def pin(self, actor, mid, enabled):
        row = self.get(mid)
        if (
            not row
            or row["subject_id"] != actor
            or row["status"] in {"retracted", "disputed", "superseded"}
        ):
            return False
        with self.repo.transaction():
            self.db.execute(
                "UPDATE mv2_memories SET protected=?,revision=revision+1 WHERE scope_id=? AND id=?",
                (int(enabled), self.scope, mid),
            )
            if not enabled and row["temporal_class"] == "core":
                self.db.execute(
                    "UPDATE mv2_memories SET kind='semantic',layer='M3',temporal_class='stable_preference',half_life_days=180,base_half_life_days=180,review_at=? WHERE id=?",
                    (row["last_evidence_at"] + 360 * 86400, mid),
                )
            self._changed(mid, "pin")
        return True

    def retract_source(self, actor, sid):
        source = self.source(sid)
        if (
            not source
            or source["sender_id"] != actor
            or source["source_state"] == "retracted"
        ):
            return False
        ids = [
            r[0]
            for r in self.db.execute(
                "SELECT memory_id FROM mv2_evidence WHERE source_id=?", (sid,)
            )
        ]
        if not ids:
            record = {
                "scope_id": self.scope,
                "scope_epoch": self.control()["epoch"] + 1,
                "target_subject": None,
                "off": False,
                "memory_ids": [],
                "source_ids": [sid],
                "event_ids": [source["event_id"]],
                "created": self.now(),
                "operation": "forget",
            }
            self._registry_append(record)
            self.replay_record(record)
            return True
        return all(
            self.forget(actor, mid)
            for mid in ids
            if self.get(mid)["status"] != "retracted"
        )
