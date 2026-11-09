"""Chinese lexical/entity RRF, strict temporal filtering and send receipts."""

import json
import re
from uuid import uuid4

from .repository import terms, token_estimate
from .decay import DAY, state_at, strength
from .admission import digest


class RetrievalMixin:
    def recall(
        self,
        query,
        requester,
        *,
        mode="direct",
        subject_ids=None,
        query_time=None,
        graph_enabled=None,
    ):
        if mode not in {"ambient", "direct", "history", "deep_recall"}:
            raise ValueError("Unknown retrieval mode")
        if mode == "deep_recall" and (
            len(terms(query)) < 2
            or not re.search(r"以前|那次|记得|回忆|当时|去年|上次", query)
        ):
            mode = "direct"
        now = self.now()
        subjects = set(subject_ids or [requester])
        packet = dict(self.snapshot(), mode=mode, items=[], revisions={}, trace_id=None)
        if not self.control()["enabled"]:
            return packet
        rows = self.repo.rows(
            "SELECT * FROM mv2_memories WHERE scope_id=? AND status NOT IN ('retracted','disputed','candidate') ORDER BY updated_at DESC LIMIT 23000",
            (self.scope,),
        )
        blocked = {
            row[0]
            for row in self.db.execute(
                "SELECT subject_id FROM mv2_subject_policies WHERE scope_id=? AND enabled=0",
                (self.scope,),
            )
        }
        involved = {}
        for mid, subject in self.db.execute(
            "SELECT s.memory_id,s.subject_id FROM mv2_memory_subjects s JOIN mv2_memories m ON m.id=s.memory_id WHERE m.scope_id=?",
            (self.scope,),
        ):
            involved.setdefault(mid, set()).add(subject)
        allowed = {}
        for row in rows:
            if (
                re.search(r"我|喜欢|偏好|称呼|喝|吃|项目", query)
                and row["subject_id"] not in subjects
                and row["kind"] != "core"
            ):
                continue
            if blocked.intersection(involved.get(row["id"], {row["subject_id"]})):
                continue
            state = state_at(row, now)
            if state == "expired" or not row["value"]:
                continue
            if mode in {"ambient", "direct"} and (
                row["status"] == "superseded"
                or row["valid_to"] is not None
                or state != "active"
            ):
                continue
            if mode == "history" and state == "frozen":
                continue
            if query_time is not None and (
                (row["valid_from"] is not None and row["valid_from"] > query_time)
                or (row["valid_to"] is not None and row["valid_to"] <= query_time)
            ):
                continue
            if (
                row["kind"] == "style"
                and self.now() - row["last_evidence_at"] > 90 * DAY
            ):
                continue
            allowed[row["id"]] = row
        qt = terms(query)
        strong_terms = {t for t in qt if len(t) >= 2} or qt
        keyword = sorted(
            (
                (mid, len(strong_terms & terms(row["value"] + " " + row["summary"])))
                for mid, row in allowed.items()
            ),
            key=lambda item: (-item[1], item[0]),
        )
        keyword = [mid for mid, score in keyword if score > 0][:30]
        entity_query = bool(
            re.search(r"我|以前|记得|偏好|喜欢|称呼|喝|吃|项目|[1-9][0-9]{4,19}", query)
        )
        entity = [
            mid
            for mid, row in allowed.items()
            if entity_query and row["subject_id"] in subjects
        ][:30]
        # FTS supplies IDs only; main DB values and revisions remain authoritative.
        if strong_terms:
            match = " OR ".join(
                '"' + word.replace('"', '""') + '"'
                for word in sorted(strong_terms)[:32]
            )
            fts = self.db.execute(
                "SELECT memory_id,revision FROM mv2_fts WHERE mv2_fts MATCH ? AND scope_id=? ORDER BY rank LIMIT 30",
                (match, self.scope),
            ).fetchall()
            keyword = list(
                dict.fromkeys(
                    [
                        mid
                        for mid, rev in fts
                        if mid in allowed and allowed[mid]["revision"] == int(rev)
                    ]
                    + keyword
                )
            )[:30]
        channels = [(keyword, 1.0), (entity, 0.8)]
        scores = {}
        denominator = sum(weight for items, weight in channels if items) / 61
        for items, weight in channels:
            for rank, mid in enumerate(items, 1):
                scores[mid] = scores.get(mid, 0) + weight / (60 + rank)
        normalized = (
            {mid: min(1, score / denominator) for mid, score in scores.items()}
            if denominator
            else {}
        )
        seeds = dict(sorted(normalized.items(), key=lambda item: -item[1])[:60])
        use_graph = (
            self.options.get("graph_enabled", True)
            if graph_enabled is None
            else graph_enabled
        )
        activation, paths = (
            self.graph_expand(seeds, allowed, strong_terms)
            if use_graph
            else (seeds, {})
        )
        ranked = []
        for mid in list(activation)[:60]:
            row = allowed[mid]
            entity_fit = 1 if entity_query and row["subject_id"] in subjects else 0
            lexical_match = bool(strong_terms & terms(row["value"]))
            relevance = (
                0.55 * normalized.get(mid, 0)
                + 0.25 * entity_fit
                + 0.20 * activation.get(mid, 0)
            )
            if relevance < 0.30 or (not lexical_match and not entity_fit):
                continue
            time_fit = (
                1
                if query_time is not None
                else max(
                    0.1, 1 / (1 + max(0, now - row["last_evidence_at"]) / (180 * DAY))
                )
            )
            score = row["confidence_score"] * (
                0.60 * relevance
                + 0.15 * row["importance"]
                + 0.15 * strength(row, now)
                + 0.10 * time_fit
            )
            ranked.append(
                (
                    score,
                    mid,
                    {
                        "relevance": relevance,
                        "rrf": normalized.get(mid, 0),
                        "entity": entity_fit,
                        "graph": activation.get(mid, 0),
                    },
                )
            )
        selected, graph_only, size, predicates = [], 0, 0, {}
        for score, mid, components in sorted(ranked, reverse=True):
            row = allowed[mid]
            graph_result = mid not in seeds
            if graph_result and graph_only >= 2:
                continue
            key = (row["subject_id"], row["predicate"])
            if predicates.get(key, 0) >= 2:
                continue
            projection = self.project(row)
            item = {
                k: projection[k]
                for k in [
                    "id",
                    "revision",
                    "kind",
                    "layer",
                    "subject_id",
                    "predicate",
                    "value",
                    "polarity",
                    "assertion_mode",
                    "confidence_score",
                    "source_state",
                    "valid_from",
                    "valid_to",
                    "time_precision",
                    "status_now",
                    "strength_now",
                ]
            }
            encoded = token_estimate(json.dumps(item, ensure_ascii=False))
            # UTF8/2 is an estimate, not the provider tokenizer; never truncate a fact.
            if size + encoded > self.options.get("retrieval_token_budget", 1600):
                continue
            selected.append(item)
            size += encoded
            graph_only += int(graph_result)
            predicates[key] = predicates.get(key, 0) + 1
            if len(selected) >= 8:
                break
        packet.update(
            items=selected,
            revisions={item["id"]: item["revision"] for item in selected},
        )
        trace = {
            "snapshot": self.snapshot(),
            "mode": mode,
            "seeds": list(seeds),
            "paths": {
                mid: path
                for mid, path in paths.items()
                if mid in {x["id"] for x in selected}
            },
            "selected": [x["id"] for x in selected],
            "scores": {mid: components for _, mid, components in ranked},
            "revisions": packet["revisions"],
            "truncated": len(ranked) > len(selected),
        }
        trace_id = "T" + uuid4().hex[:20]
        with self.repo.transaction():
            self.db.execute(
                "INSERT INTO mv2_uses VALUES(?,?,?,?,0,NULL,?)",
                (trace_id, self.scope, now, json.dumps(trace), now + DAY),
            )
            self.db.execute(
                "DELETE FROM mv2_uses WHERE scope_id=? AND id NOT IN (SELECT id FROM mv2_uses WHERE scope_id=? ORDER BY created DESC LIMIT 20)",
                (self.scope, self.scope),
            )
        packet["trace_id"] = trace_id
        return packet

    def validate_packet(self, packet):
        if not packet or not self.unchanged(
            {k: packet[k] for k in ["scope_id", "scope_epoch", "policy_version"]}
        ):
            return False
        for mid, revision in packet["revisions"].items():
            row = self.get(mid)
            if (
                not row
                or row["revision"] != revision
                or not self.permitted(self.project(row)["subjects"])
            ):
                return False
            state = state_at(row, self.now())
            if state in {"expired", "retracted", "candidate", "disputed"}:
                return False
            if packet["mode"] in {"ambient", "direct"} and (
                state != "active" or row["valid_to"] is not None
            ):
                return False
        return True

    def record_use(self, packet, receipt):
        if not receipt or not self.validate_packet(packet):
            return False
        with self.repo.transaction():
            self.db.execute(
                "UPDATE mv2_uses SET delivered=1,receipt=? WHERE scope_id=? AND id=?",
                (digest(str(receipt)), self.scope, packet["trace_id"]),
            )
            for mid in packet["revisions"]:
                self.db.execute(
                    "UPDATE mv2_memories SET last_used_at=? WHERE scope_id=? AND id=?",
                    (self.now(), self.scope, mid),
                )
        return True

    def traces(self):
        rows = self.repo.rows(
            "SELECT id,created,packet,delivered FROM mv2_uses WHERE scope_id=? AND expires_at>? ORDER BY created DESC LIMIT 20",
            (self.scope, self.now()),
        )
        result = []
        for row in rows:
            packet = json.loads(row.pop("packet"))
            allowed = {
                mid
                for mid in packet["selected"] + packet["seeds"]
                if self.get(mid) and self.get(mid)["status"] != "retracted"
            }
            packet["selected"] = [x for x in packet["selected"] if x in allowed]
            packet["seeds"] = [x for x in packet["seeds"] if x in allowed]
            packet["paths"] = {
                key: path
                for key, path in packet["paths"].items()
                if key in allowed and set(path) <= allowed
            }
            row.update(packet=packet)
            result.append(row)
        return result
