"""Decaying active expression memory; archives keep originals and cloud favorites."""

import math
import time

DAY = 86400


class StickerMemory:
    def __init__(self, db, options=None):
        self.db = db
        self.options = {
            **dict(active_limit=1500, unused_days=180, half_life_days=45),
            **(options or {}),
        }
        for name in ("active_limit", "unused_days", "half_life_days"):
            value = self.options[name]
            if type(value) is not int or value < 1:
                raise ValueError("Invalid expression memory policy")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sticker_activity(group_id TEXT,token TEXT,last_seen REAL,uses INTEGER DEFAULT 0,archived REAL,PRIMARY KEY(group_id,token))"
        )
        self.db.execute(
            "INSERT OR IGNORE INTO sticker_activity(group_id,token,last_seen) SELECT group_id,id,MAX(created,last_used) FROM stickers"
        )
        self.db.commit()
        self.last_maintenance = 0

    def touch(self, group, token, *, used=False, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute(
                "INSERT INTO sticker_activity VALUES(?,?,?,?,NULL) ON CONFLICT(group_id,token) DO UPDATE SET last_seen=excluded.last_seen,uses=sticker_activity.uses+excluded.uses,archived=NULL",
                (group, token, now, int(used)),
            )
        self.last_maintenance = 0

    def strength(self, seen, uses, now=None):
        now = time.time() if now is None else now
        return (1 + min(6, math.log1p(uses or 0))) * 2 ** (
            -max(0, now - seen) / (self.options["half_life_days"] * DAY)
        )

    def maintain(self, now=None, force=False):
        now = time.time() if now is None else now
        if not force and now - self.last_maintenance < 3600:
            return
        rows = self.db.execute(
            "SELECT s.group_id,s.id,MAX(s.created,s.last_used,COALESCE(a.last_seen,0)),COALESCE(a.uses,0),a.archived,m.source FROM stickers s LEFT JOIN sticker_activity a ON a.group_id=s.group_id AND a.token=s.id LEFT JOIN sticker_meanings m ON m.group_id=s.group_id AND m.token=s.id AND m.digest=s.digest"
        ).fetchall()
        groups = {}
        for group, token, seen, uses, archived, source in rows:
            pinned = source == "owner_label"
            if pinned or (
                archived is None and now - seen <= self.options["unused_days"] * DAY
            ):
                groups.setdefault(group, []).append((token, seen, uses, pinned))
            elif archived is None:
                with self.db:
                    self.db.execute(
                        "UPDATE sticker_activity SET archived=? WHERE group_id=? AND token=?",
                        (now, group, token),
                    )
        with self.db:
            for group, items in groups.items():
                pinned = [x for x in items if x[3]]
                for token, *_ in pinned:
                    self.db.execute(
                        "UPDATE sticker_activity SET archived=NULL WHERE group_id=? AND token=?",
                        (group, token),
                    )
                candidates = sorted(
                    (x for x in items if not x[3]),
                    key=lambda x: (self.strength(x[1], x[2], now), x[1]),
                    reverse=True,
                )
                capacity = max(0, self.options["active_limit"] - len(pinned))
                for token, *_ in candidates[capacity:]:
                    self.db.execute(
                        "UPDATE sticker_activity SET archived=? WHERE group_id=? AND token=?",
                        (now, group, token),
                    )
        self.last_maintenance = now

    def stats(self, groups):
        self.maintain()
        if not groups:
            return {"active": 0, "archived": 0, **self.options}
        q = ",".join("?" for _ in groups)
        active, archived = self.db.execute(
            f"SELECT COALESCE(SUM(a.archived IS NULL),0),COALESCE(SUM(a.archived IS NOT NULL),0) FROM stickers s LEFT JOIN sticker_activity a ON a.group_id=s.group_id AND a.token=s.id WHERE s.group_id IN ({q})",
            tuple(groups),
        ).fetchone()
        return {
            "active": active,
            "archived": archived,
            **self.options,
            "cloud_delete": False,
            "owner_labels_protected": True,
        }
