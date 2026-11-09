"""Durable leases, soft learning reservations, lazy expiry and daily catch-up."""

import json
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from .decay import DAY, state_at

TZ = timezone(timedelta(hours=8))


class MaintenanceMixin:
    def lease_valid(self, lease):
        return bool(
            self.db.execute(
                "SELECT 1 FROM mv2_tasks WHERE scope_id=? AND business_date=? AND job_kind=? AND lease_owner=? AND lease_until>? AND state='running'",
                (
                    self.scope,
                    lease["business_date"],
                    lease["job_kind"],
                    lease["lease_owner"],
                    self.now(),
                ),
            ).fetchone()
        )

    def business_day(self, stamp=None):
        return (
            datetime.fromtimestamp(self.now() if stamp is None else stamp, TZ)
            .date()
            .isoformat()
        )

    def claim_task(self, business_date, kind, *, lease_seconds=300):
        owner = uuid4().hex
        with self.repo.transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO mv2_tasks(scope_id,business_date,job_kind,state,updated) VALUES(?,?,?,'pending',?)",
                (self.scope, business_date, kind, self.now()),
            )
            task = self.repo.one(
                "SELECT * FROM mv2_tasks WHERE scope_id=? AND business_date=? AND job_kind=?",
                (self.scope, business_date, kind),
            )
            if (
                task["state"] == "succeeded"
                or (task["lease_until"] and task["lease_until"] > self.now())
                or task["attempts"] >= 3
            ):
                return None
            self.db.execute(
                "UPDATE mv2_tasks SET state='running',lease_owner=?,lease_until=?,attempts=attempts+1,updated=? WHERE scope_id=? AND business_date=? AND job_kind=?",
                (
                    owner,
                    self.now() + lease_seconds,
                    self.now(),
                    self.scope,
                    business_date,
                    kind,
                ),
            )
            return dict(
                scope_id=self.scope,
                business_date=business_date,
                job_kind=kind,
                lease_owner=owner,
                snapshot=self.snapshot(),
            )

    def finish_task(self, lease, state, stats=None):
        with self.repo.transaction():
            self.db.execute(
                "UPDATE mv2_tasks SET state=?,stats=?,step=CASE WHEN ?='succeeded' THEN 10 ELSE step END,lease_until=?,updated=? WHERE scope_id=? AND business_date=? AND job_kind=? AND lease_owner=?",
                (
                    state,
                    json.dumps(stats or {}),
                    state,
                    self.now() if state == "succeeded" else self.now() + 300,
                    self.now(),
                    self.scope,
                    lease["business_date"],
                    lease["job_kind"],
                    lease["lease_owner"],
                ),
            )

    def reserve_learning(self, job_id, estimate):
        day = self.business_day()
        with self.repo.transaction():
            used = self.db.execute(
                "SELECT COALESCE(SUM(COALESCE(actual,estimated)),0) FROM mv2_learning_usage WHERE scope_id=? AND business_date=?",
                (self.scope, day),
            ).fetchone()[0]
            if (
                used + estimate > self.options.get("daily_learning_tokens", 20000)
                or estimate < 0
            ):
                return False
            try:
                self.db.execute(
                    "INSERT INTO mv2_learning_usage VALUES(?,?,?,?,NULL,'reserved')",
                    (self.scope, day, job_id, estimate),
                )
            except Exception:
                return False
            return True

    def finish_learning(self, job_id, actual, status):
        with self.repo.transaction():
            self.db.execute(
                "UPDATE mv2_learning_usage SET actual=COALESCE(?,actual),status=? WHERE scope_id=? AND job_id=?",
                (actual, status, self.scope, job_id),
            )

    def expire(self):
        now = self.now()
        with self.repo.transaction():
            raw = self.db.execute(
                "UPDATE mv2_sources SET body=NULL,source_state='expired' WHERE scope_id=? AND expires_at<=? AND body IS NOT NULL AND source_state!='retracted'",
                (self.scope, now),
            ).rowcount
            expired = self.repo.rows(
                "SELECT * FROM mv2_memories WHERE scope_id=? AND expires_at IS NOT NULL AND expires_at<=? AND value!=''",
                (self.scope, now),
            )
            for row in expired:
                self.db.execute(
                    "UPDATE mv2_memories SET value='',summary='',status='frozen',revision=revision+1 WHERE id=?",
                    (row["id"],),
                )
                self.db.execute(
                    "DELETE FROM mv2_fts WHERE memory_id=? AND scope_id=?",
                    (row["id"], self.scope),
                )
                self.db.execute(
                    "DELETE FROM mv2_edges WHERE scope_id=? AND (src=? OR dst=?)",
                    (self.scope, row["id"], row["id"]),
                )
                self._changed(row["id"], "expired")
            edge_count = 0
            for edge in self.repo.rows(
                "SELECT * FROM mv2_edges WHERE scope_id=?", (self.scope,)
            ):
                weight = edge["weight"] * 2 ** (
                    -max(0, now - edge["anchor"]) / (edge["half_life"] * DAY)
                )
                if weight < 0.1:
                    self.db.execute(
                        "DELETE FROM mv2_edges WHERE scope_id=? AND src=? AND dst=? AND type=?",
                        (self.scope, edge["src"], edge["dst"], edge["type"]),
                    )
                    edge_count += 1
            self.db.execute(
                "DELETE FROM mv2_uses WHERE scope_id=? AND expires_at<=?",
                (self.scope, now),
            )
            self.db.execute(
                "DELETE FROM mv2_outbox WHERE scope_id=? AND created<? AND indexed=1 AND seq<(SELECT COALESCE(MAX(seq),0) FROM mv2_outbox WHERE scope_id=?)",
                (self.scope, now - 7 * DAY, self.scope),
            )
        return {
            "raw_expired": raw,
            "memories_expired": len(expired),
            "edges_pruned": edge_count,
        }

    def maintain(self, business_date):
        lease = self.claim_task(business_date, "maintenance")
        if not lease:
            return {"result": "NO_CHANGE"}
        try:
            stats = self.expire()
            with self.repo.transaction():
                if not self.unchanged(lease["snapshot"]):
                    self.finish_task(lease, "cancelled", {"reason": "stale_epoch"})
                    return {"result": "CANCELLED"}
                states = {"active": 0, "dormant": 0, "frozen": 0, "expired": 0}
                candidates = []
                for row in self.repo.rows(
                    "SELECT * FROM mv2_memories WHERE scope_id=? AND status IN ('active','dormant','frozen')",
                    (self.scope,),
                ):
                    state = state_at(row, self.now())
                    states[state] = states.get(state, 0) + 1
                    if state != "expired" and row["status"] != state:
                        self.db.execute(
                            "UPDATE mv2_memories SET status=? WHERE id=?",
                            (state, row["id"]),
                        )
                    if (
                        row["kind"] == "episode"
                        and row["predicate"] != "event_gist"
                        and row["updated_at"] >= self.now() - 7 * DAY
                    ):
                        candidates.append(row["id"])
                gists = 0
                for mid in candidates[:20]:
                    if not self.db.execute(
                        "SELECT 1 FROM mv2_dependencies WHERE parent_id=? AND relation_type='derived_from'",
                        (mid,),
                    ).fetchone():
                        gists += bool(self.derive_gist([mid]))
                self.repo.rebuild_index(self.scope)
                stats.update(
                    states=states,
                    gists_created=gists,
                    result="NO_CHANGE"
                    if not any(
                        [
                            gists,
                            stats["raw_expired"],
                            stats["memories_expired"],
                            stats["edges_pruned"],
                        ]
                    )
                    else "UPDATED",
                )
                self.finish_task(lease, "succeeded", stats)
                self.repo.event(self.scope, None, None, "maintenance", self.now())
            return stats
        except BaseException:
            self.finish_task(lease, "failed", {"reason": "maintenance_error"})
            raise

    def catch_up(self):
        local = datetime.fromtimestamp(self.now(), TZ)
        end = local.date() - timedelta(
            days=1 if (local.hour, local.minute) >= (3, 30) else 2
        )
        return [
            self.maintain((end - timedelta(days=offset)).isoformat())
            for offset in reversed(range(7))
        ]

    def maintenance_status(self):
        used = self.db.execute(
            "SELECT COALESCE(SUM(COALESCE(actual,estimated)),0) FROM mv2_learning_usage WHERE scope_id=? AND business_date=?",
            (self.scope, self.business_day()),
        ).fetchone()[0]
        return {
            "scope_id": self.scope,
            "embedding": "none_keyword_fallback",
            "pending_sources": len(self.pending(1000)),
            "today_learning_reserved_tokens": used,
            "today_learning_budget": self.options.get("daily_learning_tokens", 20000),
            "index_backlog": self.db.execute(
                "SELECT COUNT(*) FROM mv2_outbox WHERE scope_id=? AND indexed=0 AND memory_id IS NOT NULL",
                (self.scope,),
            ).fetchone()[0],
            "tasks": self.repo.rows(
                "SELECT business_date,job_kind,state,step,attempts,updated FROM mv2_tasks WHERE scope_id=? ORDER BY updated DESC LIMIT 10",
                (self.scope,),
            ),
        }
