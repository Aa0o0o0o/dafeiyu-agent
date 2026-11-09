"""Idempotent V1 import. Missing provenance stays candidate, never guessed."""

from .admission import digest, safe_source
from .decay import DAY


def migrate_legacy(memory):
    report = {
        "sources_imported": 0,
        "automatic_imported": 0,
        "explicit_quarantined": 0,
        "missing_provenance": 0,
        "already_migrated": 0,
        "old_raw_not_relearned": True,
    }
    private = ":group:" not in memory.scope
    actor = memory.owner_id if private else None
    if not memory._has_table("ltm_journal"):
        return report
    with memory.repo.transaction():
        if memory.db.execute(
            "SELECT 1 FROM mv2_migrations WHERE scope_id=? AND legacy_key='__v1_archive__'",
            (memory.scope,),
        ).fetchone():
            report["already_migrated"] = 1
            return report
        old_control = memory.repo.one(
            "SELECT paused FROM ltm_controls WHERE scope=?", (memory.scope,)
        )
        if actor and old_control and old_control["paused"]:
            memory.db.execute(
                "INSERT OR REPLACE INTO mv2_subject_policies VALUES(?,?,0,1)",
                (memory.scope, actor),
            )
        if actor:
            for row in memory.repo.rows(
                "SELECT * FROM ltm_journal WHERE scope=? AND role='user' AND body IS NOT NULL AND created>? ORDER BY seq",
                (memory.scope, memory.now() - 7 * DAY),
            ):
                source = memory.observe(
                    {
                        "event_id": row["event_id"],
                        "sender_id": actor,
                        "subject_ids": [actor],
                        "text": row["body"],
                        "event_time": row["created"],
                    }
                )
                if source:
                    memory.db.execute(
                        "UPDATE mv2_sources SET processed=1 WHERE id=?", (source,)
                    )
                    report["sources_imported"] += 1
        rows = memory.repo.rows(
            "SELECT * FROM ltm_items WHERE scope=? AND status IN ('active','frozen') AND expires>?",
            (memory.scope, memory.now()),
        )
        for old in rows:
            key = "A" + str(old["id"])
            if memory.db.execute(
                "SELECT 1 FROM mv2_migrations WHERE scope_id=? AND legacy_key=?",
                (memory.scope, key),
            ).fetchone():
                report["already_migrated"] += 1
                continue
            if not safe_source(old["value"]):
                continue
            temporal = (
                "stable_preference"
                if old["predicate"] in {"nickname", "reply_style", "likes", "dislikes"}
                else "episode"
                if old["predicate"] == "episode"
                else "temporary_state"
            )
            item = {
                "kind": "episode" if temporal == "episode" else "semantic",
                "subject_ids": [actor or "unbound"],
                "value": old["value"],
                "assertion_mode": "self_report",
                "confidence_score": old["confidence"],
                "importance": old["importance"],
                "temporal_class": temporal,
            }
            mid = memory._new_memory(item, old["predicate"], old["created"])
            valid_sources = []
            for src in memory.repo.rows(
                "SELECT event_id FROM ltm_sources WHERE scope=? AND memory_id=?",
                (memory.scope, old["id"]),
            ):
                sid = "E" + digest(memory.scope, src["event_id"], 1)[:32]
                source = memory.source(sid)
                if (
                    source
                    and source["body"]
                    and old["quote"] in source["body"]
                    and old["value"] in old["quote"]
                ):
                    valid_sources.append(source)
            status = old["status"] if actor and valid_sources else "candidate"
            memory.db.execute(
                "UPDATE mv2_memories SET status=?,strength_at_anchor=?,anchor_at=?,half_life_days=?,base_half_life_days=?,expires_at=?,created_at=?,updated_at=? WHERE id=?",
                (
                    status,
                    old["strength"],
                    old["anchor"],
                    old["half_life"],
                    old["half_life"],
                    old["expires"],
                    old["created"],
                    old["created"],
                    mid,
                ),
            )
            for source in valid_sources:
                memory.db.execute(
                    "INSERT OR IGNORE INTO mv2_evidence VALUES(?,?,?,?,?)",
                    (mid, source["id"], digest(source["id"]), "support", memory.now()),
                )
            memory.db.execute(
                "INSERT INTO mv2_migrations VALUES(?,?,?,?)",
                (
                    memory.scope,
                    key,
                    mid,
                    "verified" if status != "candidate" else "missing_provenance",
                ),
            )
            memory._changed(mid, "migrate")
            report["automatic_imported"] += 1
            report["missing_provenance"] += status == "candidate"
        for old in memory.repo.rows(
            "SELECT * FROM memories WHERE scope=?", (memory.scope,)
        ):
            key = str(old["id"])
            if memory.db.execute(
                "SELECT 1 FROM mv2_migrations WHERE scope_id=? AND legacy_key=?",
                (memory.scope, key),
            ).fetchone() or not safe_source(old["text"]):
                continue
            item = {
                "kind": "semantic",
                "subject_ids": [actor or "unbound"],
                "value": old["text"],
                "assertion_mode": "self_report",
                "confidence_score": 0.75,
                "importance": 0.7,
                "temporal_class": "stable_preference",
            }
            mid = memory._new_memory(item, "explicit_legacy", memory.now())
            memory.db.execute(
                "UPDATE mv2_memories SET status='candidate',confidence_basis='legacy_source_missing',expires_at=? WHERE id=?",
                (memory.now() + 30 * DAY, mid),
            )
            memory.db.execute(
                "INSERT INTO mv2_migrations VALUES(?,?,?,?)",
                (memory.scope, key, mid, "missing_provenance"),
            )
            memory._changed(mid, "migrate")
            report["explicit_quarantined"] += 1
        memory.repo.rebuild_index(memory.scope)
        # The staged V2 database has one body authority. Original data survives
        # only in the pre-switch, expiring backup, not a dormant second writer.
        memory.db.execute(
            "UPDATE ltm_journal SET body=NULL WHERE scope=?", (memory.scope,)
        )
        memory.db.execute(
            "UPDATE ltm_items SET value='',quote='',status='migrated_v2' WHERE scope=?",
            (memory.scope,),
        )
        memory.db.execute("DELETE FROM memories WHERE scope=?", (memory.scope,))
        memory.db.execute(
            "UPDATE ltm_controls SET paused=1 WHERE scope=?", (memory.scope,)
        )
        memory.db.execute(
            "INSERT INTO mv2_migrations VALUES(?,'__v1_archive__',NULL,'archived_no_second_writer')",
            (memory.scope,),
        )
    memory.replay_registry()
    return report
