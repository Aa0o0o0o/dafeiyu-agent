"""Sparse associations, bounded two-hop activation; associations are not evidence."""

import re

from .repository import terms
from .decay import DAY, state_at


class GraphMixin:
    def build_edges(self, mid, source_id=None):
        row = self.get(mid)
        if not row or row["status"] in {"candidate", "retracted"}:
            return
        candidates = self.repo.rows(
            "SELECT * FROM mv2_memories WHERE scope_id=? AND id!=? AND subject_id=? AND status IN ('active','dormant','frozen') ORDER BY updated_at DESC LIMIT 20",
            (self.scope, mid, row["subject_id"]),
        )
        for other in candidates:
            common = terms(row["value"]) & terms(other["value"])
            bigrams = {x for x in common if len(x) >= 2}
            if not bigrams or state_at(other, self.now()) == "expired":
                continue
            source = self.db.execute(
                "SELECT source_id FROM mv2_evidence WHERE memory_id=? LIMIT 1", (mid,)
            ).fetchone()
            for src, dst in [(mid, other["id"]), (other["id"], mid)]:
                previous = self.repo.one(
                    "SELECT * FROM mv2_edges WHERE scope_id=? AND src=? AND dst=? AND type='same_topic'",
                    (self.scope, src, dst),
                )
                if previous and source_id and previous["source_id"] != source_id:
                    evidence = self.source(source_id)
                    if (
                        evidence
                        and evidence["event_time"] >= previous["anchor"] + DAY
                        and previous["last_boost_day"]
                        != self.business_day(evidence["event_time"])
                    ):
                        weight = previous["weight"] * 2 ** (
                            -(evidence["event_time"] - previous["anchor"])
                            / (previous["half_life"] * DAY)
                        )
                        self.db.execute(
                            "UPDATE mv2_edges SET weight=?,anchor=?,source_id=?,last_boost_day=? WHERE scope_id=? AND src=? AND dst=? AND type='same_topic'",
                            (
                                min(1, weight + 0.1),
                                evidence["event_time"],
                                source_id,
                                self.business_day(evidence["event_time"]),
                                self.scope,
                                src,
                                dst,
                            ),
                        )
                    continue
                count = self.db.execute(
                    "SELECT COUNT(*) FROM mv2_edges WHERE scope_id=? AND src=?",
                    (self.scope, src),
                ).fetchone()[0]
                if count < 12:
                    self.db.execute(
                        "INSERT OR IGNORE INTO mv2_edges VALUES(?,?,?,'same_topic',.3,?,30,?,?)",
                        (
                            self.scope,
                            src,
                            dst,
                            self.now(),
                            source_id or (source[0] if source else None),
                            self.business_day(self.now()),
                        ),
                    )

    def graph_expand(self, seeds, allowed, query_terms):
        activation = {mid: value for mid, value in seeds.items()}
        frontier = dict(activation)
        paths = {}
        for _ in range(2):
            next_frontier = {}
            for src, value in frontier.items():
                edges = self.repo.rows(
                    "SELECT * FROM mv2_edges WHERE scope_id=? AND src=? LIMIT 12",
                    (self.scope, src),
                )
                weighted = [
                    (
                        e,
                        e["weight"]
                        * 2
                        ** (-max(0, self.now() - e["anchor"]) / (e["half_life"] * DAY)),
                    )
                    for e in edges
                ]
                denominator = max(1, sum(w for _, w in weighted))
                for edge, weight in weighted:
                    dst = edge["dst"]
                    if (
                        dst not in allowed
                        or weight < 0.1
                        or not query_terms.intersection(terms(allowed[dst]["value"]))
                    ):
                        continue
                    if dst not in activation and len(activation) >= 60:
                        continue
                    contribution = 0.4 * value * weight / denominator
                    next_frontier[dst] = next_frontier.get(dst, 0) + contribution
                    activation[dst] = min(1, activation.get(dst, 0) + contribution)
                    paths.setdefault(dst, paths.get(src, [src]) + [dst])
            frontier = next_frontier
        return activation, paths

    def derive_gist(self, parent_ids):
        parents = [self.get(mid) for mid in parent_ids]
        if not 1 <= len(parents) <= 10 or any(
            not p
            or p["kind"] != "episode"
            or state_at(p, self.now())
            in {"expired", "retracted", "candidate", "superseded", "disputed"}
            for p in parents
        ):
            return None
        subjects = {p["subject_id"] for p in parents}
        if len(subjects) != 1:
            return None
        existing = self.db.execute(
            "SELECT child_id FROM mv2_dependencies WHERE parent_id=? AND relation_type='derived_from'",
            (parents[0]["id"],),
        ).fetchone()
        if existing:
            return existing[0]
        # Do not embellish a shared experience; carry only the grounded gist.
        source_important = any(
            source
            and source["body"]
            and re.search(
                r"(?<!不)(?<!没那么)(?<!并非)重要|难忘|值得记住", source["body"]
            )
            for parent in parents
            for evidence in self.repo.rows(
                "SELECT source_id FROM mv2_evidence WHERE memory_id=?", (parent["id"],)
            )
            for source in [self.source(evidence["source_id"])]
        )
        important = any(
            p["temporal_class"] == "important_episode" for p in parents
        ) or (source_important and min(p["importance"] for p in parents) >= 0.8)
        span = self.repo.one(
            "SELECT start,stop FROM mv2_gist_spans WHERE parent_id=?",
            (parents[0]["id"],),
        )
        phrase = parents[0]["value"][span["start"] : span["stop"]] if span else None
        if not phrase:
            match = re.search(
                r"(?:排查|排错|编程|答辩|比赛|旅行|项目|完成|讨论|学习|开发)[\u3400-\u9fffA-Za-z]{1,16}",
                parents[0]["value"],
            )
            phrase = match[0] if match else parents[0]["value"][:48]
        item = {
            "kind": "episode",
            "subject_ids": list(subjects),
            "value": "曾提到：" + phrase,
            "assertion_mode": "self_report",
            "confidence_score": min(p["confidence_score"] for p in parents),
            "importance": min(p["importance"] for p in parents),
            "temporal_class": "important_episode" if important else "episode",
        }
        with self.repo.transaction():
            mid = self._new_memory(
                item,
                "event_gist",
                min(p["event_time_start"] or p["anchor_at"] for p in parents),
            )
            self.db.execute(
                "UPDATE mv2_memories SET independent_evidence_count=0 WHERE id=?",
                (mid,),
            )
            for parent in parents:
                self.db.execute(
                    "INSERT OR IGNORE INTO mv2_dependencies VALUES(?,?,'derived_from')",
                    (parent["id"], mid),
                )
                for evidence in self.repo.rows(
                    "SELECT * FROM mv2_evidence WHERE memory_id=?", (parent["id"],)
                ):
                    self.db.execute(
                        "INSERT OR IGNORE INTO mv2_evidence VALUES(?,?,?,?,?)",
                        (
                            mid,
                            evidence["source_id"],
                            evidence["evidence_group_id"],
                            "support",
                            self.now(),
                        ),
                    )
            self._changed(mid, "gist")
            self.db.execute(
                "UPDATE mv2_memories SET independent_evidence_count=(SELECT COUNT(DISTINCT evidence_group_id) FROM mv2_evidence WHERE memory_id=?) WHERE id=?",
                (mid, mid),
            )
            self.build_edges(mid)
        return mid
