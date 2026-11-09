"""Additive schema, serialized writes and rebuildable Chinese term index."""

import json
import re
from contextlib import contextmanager
from uuid import uuid4

SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS mv2_schema(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO mv2_schema VALUES(2);
CREATE TABLE IF NOT EXISTS mv2_scopes(scope_id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, platform_instance_id TEXT NOT NULL, epoch INTEGER NOT NULL DEFAULT 0, policy_version INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mv2_subject_policies(scope_id TEXT,subject_id TEXT,enabled INTEGER NOT NULL DEFAULT 1,epoch INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(scope_id,subject_id),FOREIGN KEY(scope_id) REFERENCES mv2_scopes(scope_id));
CREATE TABLE IF NOT EXISTS mv2_sources(id TEXT PRIMARY KEY,scope_id TEXT NOT NULL,event_id TEXT NOT NULL,revision INTEGER NOT NULL,sender_id TEXT NOT NULL,body TEXT,event_time REAL NOT NULL,received_at REAL NOT NULL,expires_at REAL NOT NULL,source_state TEXT NOT NULL,source_kind TEXT NOT NULL,session_id TEXT NOT NULL,processed INTEGER NOT NULL DEFAULT 0,UNIQUE(scope_id,event_id,revision));
CREATE INDEX IF NOT EXISTS mv2_source_pending ON mv2_sources(scope_id,processed,event_time);
CREATE TABLE IF NOT EXISTS mv2_source_subjects(source_id TEXT,subject_id TEXT,PRIMARY KEY(source_id,subject_id),FOREIGN KEY(source_id) REFERENCES mv2_sources(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS mv2_memories(id TEXT PRIMARY KEY,scope_id TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,kind TEXT NOT NULL,layer TEXT NOT NULL,status TEXT NOT NULL,subject_id TEXT NOT NULL,predicate TEXT NOT NULL,value TEXT NOT NULL,summary TEXT NOT NULL,polarity TEXT NOT NULL,assertion_mode TEXT NOT NULL,confidence_score REAL NOT NULL,confidence_basis TEXT NOT NULL,importance REAL NOT NULL,temporal_class TEXT NOT NULL,event_time_start REAL,event_time_end REAL,time_precision TEXT NOT NULL,valid_from REAL,valid_to REAL,recorded_at REAL NOT NULL,superseded_at REAL,last_evidence_at REAL NOT NULL,independent_evidence_count INTEGER NOT NULL,strength_at_anchor REAL NOT NULL,anchor_at REAL NOT NULL,half_life_days REAL,base_half_life_days REAL,last_used_at REAL,protected INTEGER NOT NULL DEFAULT 0,expires_at REAL,review_at REAL,scope_epoch INTEGER NOT NULL,policy_version INTEGER NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS mv2_memory_scope ON mv2_memories(scope_id,subject_id,predicate,status);
CREATE TABLE IF NOT EXISTS mv2_memory_subjects(memory_id TEXT,subject_id TEXT,role TEXT NOT NULL,PRIMARY KEY(memory_id,subject_id),FOREIGN KEY(memory_id) REFERENCES mv2_memories(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS mv2_evidence(memory_id TEXT,source_id TEXT,evidence_group_id TEXT NOT NULL,stance TEXT NOT NULL DEFAULT 'support',created REAL NOT NULL,PRIMARY KEY(memory_id,source_id),UNIQUE(memory_id,evidence_group_id),FOREIGN KEY(memory_id) REFERENCES mv2_memories(id) ON DELETE CASCADE,FOREIGN KEY(source_id) REFERENCES mv2_sources(id));
CREATE TABLE IF NOT EXISTS mv2_dependencies(parent_id TEXT,child_id TEXT,relation_type TEXT NOT NULL,PRIMARY KEY(parent_id,child_id,relation_type),FOREIGN KEY(parent_id) REFERENCES mv2_memories(id) ON DELETE CASCADE,FOREIGN KEY(child_id) REFERENCES mv2_memories(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS mv2_entities(scope_id TEXT,id TEXT,entity_type TEXT NOT NULL,PRIMARY KEY(scope_id,id));
CREATE TABLE IF NOT EXISTS mv2_aliases(scope_id TEXT,entity_id TEXT,alias TEXT NOT NULL,PRIMARY KEY(scope_id,entity_id,alias));
CREATE TABLE IF NOT EXISTS mv2_edges(scope_id TEXT,src TEXT,dst TEXT,type TEXT,weight REAL NOT NULL,anchor REAL NOT NULL,half_life REAL NOT NULL,source_id TEXT,last_boost_day TEXT,PRIMARY KEY(scope_id,src,dst,type),FOREIGN KEY(src) REFERENCES mv2_memories(id) ON DELETE CASCADE,FOREIGN KEY(dst) REFERENCES mv2_memories(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS mv2_uses(id TEXT PRIMARY KEY,scope_id TEXT NOT NULL,created REAL NOT NULL,packet TEXT NOT NULL,delivered INTEGER NOT NULL DEFAULT 0,receipt TEXT,expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mv2_tasks(scope_id TEXT,business_date TEXT,job_kind TEXT,state TEXT NOT NULL,lease_owner TEXT,lease_until REAL,step INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,stats TEXT NOT NULL DEFAULT '{}',updated REAL NOT NULL,PRIMARY KEY(scope_id,business_date,job_kind));
CREATE TABLE IF NOT EXISTS mv2_learning_usage(scope_id TEXT,business_date TEXT,job_id TEXT,estimated INTEGER NOT NULL,actual INTEGER,status TEXT NOT NULL,PRIMARY KEY(scope_id,business_date,job_id));
CREATE TABLE IF NOT EXISTS mv2_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,scope_id TEXT NOT NULL,memory_id TEXT,revision INTEGER,kind TEXT NOT NULL,created REAL NOT NULL,indexed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS mv2_index_revisions(scope_id TEXT PRIMARY KEY,version INTEGER NOT NULL,applied_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS mv2_tombstones(scope_id TEXT,target_type TEXT,target_id TEXT,epoch INTEGER NOT NULL,created REAL NOT NULL,PRIMARY KEY(scope_id,target_type,target_id));
CREATE TABLE IF NOT EXISTS mv2_suppression(scope_id TEXT,event_id TEXT,PRIMARY KEY(scope_id,event_id));
CREATE TABLE IF NOT EXISTS mv2_proposals(scope_id TEXT,extractor_version TEXT,output_hash TEXT,created REAL NOT NULL,PRIMARY KEY(scope_id,extractor_version,output_hash));
CREATE TABLE IF NOT EXISTS mv2_layouts(viewer_id TEXT,scope_id TEXT,version INTEGER,layout TEXT NOT NULL,PRIMARY KEY(viewer_id,scope_id,version));
CREATE TABLE IF NOT EXISTS mv2_migrations(scope_id TEXT,legacy_key TEXT,new_id TEXT,classification TEXT NOT NULL,PRIMARY KEY(scope_id,legacy_key));
CREATE TABLE IF NOT EXISTS mv2_pending_actions(id TEXT PRIMARY KEY,scope_id TEXT,subject_id TEXT,state TEXT NOT NULL,summary TEXT NOT NULL,created REAL NOT NULL,receipt TEXT);
CREATE TABLE IF NOT EXISTS mv2_registry_applied(scope_id TEXT,record_hash TEXT,PRIMARY KEY(scope_id,record_hash));
CREATE TABLE IF NOT EXISTS mv2_blocked_aliases(scope_id TEXT,subject_id TEXT,length INTEGER,alias_hash TEXT,PRIMARY KEY(scope_id,subject_id,alias_hash));
CREATE TABLE IF NOT EXISTS mv2_style_activations(scope_id TEXT,memory_id TEXT,business_date TEXT,PRIMARY KEY(scope_id,memory_id));
CREATE TABLE IF NOT EXISTS mv2_viewer_prefs(viewer_id TEXT,bot_id TEXT,last_scope TEXT,updated REAL,PRIMARY KEY(viewer_id,bot_id));
CREATE TABLE IF NOT EXISTS mv2_gist_spans(parent_id TEXT PRIMARY KEY,start INTEGER NOT NULL,stop INTEGER NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS mv2_fts USING fts5(scope_id UNINDEXED,memory_id UNINDEXED,revision UNINDEXED,terms,tokenize='unicode61');
"""


def terms(text):
    # Matching ingestion/query tokenization; Chinese 1-char fallback and bigrams.
    result = set(re.findall(r"[a-z0-9_]{2,}", text.lower()))
    for word in re.findall(r"[\u3400-\u9fff]+", text):
        result.update(word)
        result.update(word[i : i + 2] for i in range(len(word) - 1))
    return (
        result
        - set("我你他她的是了在有不和与这那")
        - {"什么", "记得", "现在", "可以", "一下", "这个", "今天"}
    )


def token_estimate(text):
    """Explicit UTF8/2 estimate, not a provider tokenizer or invoice."""
    return (len(text.encode("utf-8")) + 1) // 2


class Repository:
    def __init__(self, db):
        self.db = db
        db.executescript(SCHEMA)

    def rows(self, sql, args=()):
        cur = self.db.execute(sql, args)
        columns = [c[0] for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:
            name = "mv2_" + uuid4().hex
            self.db.execute("SAVEPOINT " + name)
            try:
                yield
                self.db.execute("RELEASE " + name)
            except BaseException:
                self.db.execute("ROLLBACK TO " + name)
                self.db.execute("RELEASE " + name)
                raise
        else:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def insert(self, table, values):
        self.db.execute(
            f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)})",
            tuple(values.values()),
        )

    def event(self, scope, mid, revision, kind, now):
        self.db.execute(
            "INSERT INTO mv2_outbox(scope_id,memory_id,revision,kind,created) VALUES(?,?,?,?,?)",
            (scope, mid, revision, kind, now),
        )

    def graph_revision(self, scope):
        return self.db.execute(
            "SELECT COALESCE(MAX(seq),0) FROM mv2_outbox WHERE scope_id=?", (scope,)
        ).fetchone()[0]

    def index(self, memory):
        self.db.execute(
            "DELETE FROM mv2_fts WHERE memory_id=? AND scope_id=?",
            (memory["id"], memory["scope_id"]),
        )
        if memory["status"] not in {"retracted", "disputed"} and memory["value"]:
            self.db.execute(
                "INSERT INTO mv2_fts VALUES(?,?,?,?)",
                (
                    memory["scope_id"],
                    memory["id"],
                    memory["revision"],
                    " ".join(sorted(terms(memory["summary"] + " " + memory["value"]))),
                ),
            )
        self.db.execute(
            "UPDATE mv2_outbox SET indexed=1 WHERE scope_id=? AND memory_id=?",
            (memory["scope_id"], memory["id"]),
        )

    def rebuild_index(self, scope):
        with self.transaction():
            self.db.execute("DELETE FROM mv2_fts WHERE scope_id=?", (scope,))
            for memory in self.rows(
                "SELECT * FROM mv2_memories WHERE scope_id=? AND status!='retracted'",
                (scope,),
            ):
                self.index(memory)
            self.db.execute(
                "INSERT OR REPLACE INTO mv2_index_revisions VALUES(?,1,?)",
                (scope, self.graph_revision(scope)),
            )

    def stats_json(self, value):
        return json.dumps(value, separators=(",", ":"))
