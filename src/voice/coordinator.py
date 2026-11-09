"""One voice presentation per logical reply; no additional chat model."""

import asyncio
import hashlib
import json
import random
import re
import time
import uuid
from pathlib import Path

import httpx
from .media import validate_wav
from .delivery import plan_delivery
from .normalize import spoken_text, voice_requested, voice_refused


class VoiceCoordinator:
    def __init__(self, service, root, options):
        self.service, self.root, self.options = service, Path(root), options
        self.directory = (
            self.root
            / "runtime/voice"
            / hashlib.sha256(service.scope.encode()).hexdigest()
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        self.client = httpx.AsyncClient(
            base_url="http://127.0.0.1:6210", trust_env=False, timeout=90
        )
        self.random = random.random
        self.last_error = None
        self.delivery_factor = 1.0
        self.ready = False
        self.quote_cache = {}
        self.db = service.store.db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS voice_job_metadata(id TEXT PRIMARY KEY,data TEXT);
        CREATE TABLE IF NOT EXISTS voice_jobs(id TEXT PRIMARY KEY,scope TEXT,turn_id TEXT,actor TEXT,state TEXT,
          snapshot TEXT,created REAL,deadline REAL,path TEXT,voice_receipt TEXT,text_receipt TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS voice_prefs(scope TEXT,actor TEXT,enabled INTEGER,caption INTEGER,PRIMARY KEY(scope,actor));
        CREATE TABLE IF NOT EXISTS voice_quote_refs(scope TEXT,message_id TEXT,digest TEXT,created REAL,PRIMARY KEY(scope,message_id));
        """)
        self.db.execute(
            "UPDATE voice_jobs SET state='expired' WHERE scope=? AND state IN ('pending','running','ready','delivering')",
            (service.scope,),
        )
        self.db.commit()
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS voice_modes(scope TEXT PRIMARY KEY,mode TEXT)"
        )
        mode = self.db.execute(
            "SELECT mode FROM voice_modes WHERE scope=?", (service.scope,)
        ).fetchone()
        self.options = dict(
            options, mode=mode[0] if mode else options.get("mode", "on_demand")
        )
        if service.v2:
            service.v2.voice_coordinator = self
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS voice_profiles(scope TEXT PRIMARY KEY,profile TEXT)"
        )
        profile = self.db.execute(
            "SELECT profile FROM voice_profiles WHERE scope=?", (service.scope,)
        ).fetchone()
        if profile and (
            not options.get("allowed_profiles")
            or profile[0] in options["allowed_profiles"]
        ):
            self.options["profile"] = profile[0]
        self.purge()

    def should_speak(self, incoming, text, actor):
        """Explicit wishes and actor opt-out take precedence over short-chat sampling."""
        if not self.preferences(actor)[0] or self.options.get("mode") == "text":
            return False
        if voice_refused(incoming):
            return False
        if voice_requested(incoming):
            return True
        if self.options.get("mode") != "automatic":
            return False
        dialogue = getattr(self.service, "dialogue_context", None)
        if isinstance(dialogue, dict) and dialogue.get("media_hint") == "text":
            return False
        if re.search(
            r"代码|公式|表格|详细|完整|严肃|正经|论文|报告|教程|分析", incoming
        ):
            return False
        try:
            spoken_text(text, maximum=self.options.get("automatic_max_chars", 100))
        except ValueError:
            return False
        if re.search(r"(?m)^\s*(?:[-*] |\d+[.、] )", text):
            return False
        return self.random() < self.options.get("automatic_probability", 0)

    def headers(self):
        import win32crypt

        secret = json.loads((self.root / "config/voice-secret.json").read_text())
        token = win32crypt.CryptUnprotectData(
            bytes.fromhex(secret["dpapi"]), None, None, None, 0
        )[1].decode()
        return {"Authorization": "Bearer " + token}

    def preferences(self, actor):
        row = self.db.execute(
            "SELECT enabled,caption FROM voice_prefs WHERE scope=? AND actor=?",
            (self.service.scope, actor),
        ).fetchone()
        return row or (1, int(self.options.get("caption_default", False)))

    def set_preferences(self, actor, enabled=None, caption=None):
        old = self.preferences(actor)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO voice_prefs VALUES(?,?,?,?)",
                (
                    self.service.scope,
                    actor,
                    old[0] if enabled is None else enabled,
                    old[1] if caption is None else caption,
                ),
            )

    def purge(self):
        for row in self.db.execute(
            "SELECT id,path FROM voice_jobs WHERE scope=? AND (created<? OR state IN ('cancelled','expired','failed'))",
            (self.service.scope, time.time() - 86400),
        ).fetchall():
            if row[1]:
                path = Path(row[1])
                if (
                    path.parent.resolve() == self.directory.resolve()
                    and not path.is_symlink()
                ):
                    path.unlink(missing_ok=True)
            with self.db:
                self.db.execute("DELETE FROM voice_job_metadata WHERE id=?", (row[0],))
                self.db.execute(
                    "UPDATE voice_jobs SET path=NULL,state='expired' WHERE id=?",
                    (row[0],),
                )

    def invalidate(self, actor=None):
        with self.db:
            self.db.execute(
                "UPDATE voice_jobs SET state='cancelled' WHERE scope=?"
                + (" AND actor=?" if actor else ""),
                (self.service.scope, actor) if actor else (self.service.scope,),
            )
        self.purge()

    def remember_quote(self, receipt, spoken):
        """Keep delivered speech in this session; persist only a digest for private history."""
        if not receipt or not isinstance(spoken, str) or not 1 <= len(spoken) <= 200:
            return
        now = time.time()
        ttl = 1200 if ':group:' in self.service.scope else 86400
        self.quote_cache = {k: v for k, v in self.quote_cache.items() if now - v[0] < ttl}
        self.quote_cache[str(receipt)] = (now, spoken)
        while len(self.quote_cache) > 256:
            self.quote_cache.pop(next(iter(self.quote_cache)))
        if ':group:' not in self.service.scope and self.service.persistent_history_allowed():
            digest = hashlib.sha256(spoken.encode('utf-8')).hexdigest()
            with self.db:
                self.db.execute("DELETE FROM voice_quote_refs WHERE scope=? AND created<?",
                                (self.service.scope, now - 7*86400))
                self.db.execute("INSERT OR REPLACE INTO voice_quote_refs VALUES(?,?,?,?)",
                                (self.service.scope, str(receipt), digest, now))

    def known_quote(self, receipt):
        """Return verified speech, never guessing a message from nearby timestamps."""
        now = time.time()
        cached = self.quote_cache.get(str(receipt))
        ttl = 1200 if ':group:' in self.service.scope else 86400
        if cached and now - cached[0] < ttl:
            return cached[1]
        if ':group:' in self.service.scope or not self.service.persistent_history_allowed():
            return None
        row = self.db.execute(
            "SELECT digest FROM voice_quote_refs WHERE scope=? AND message_id=? AND created>?",
            (self.service.scope, str(receipt), now - 7*86400),
        ).fetchone()
        if not row:
            return None
        for item in self.service.store.history():
            if item.get('role') != 'assistant':
                continue
            try:
                candidate = spoken_text(item.get('content', ''))
            except (TypeError, ValueError):
                continue
            if hashlib.sha256(candidate.encode('utf-8')).hexdigest() == row[0]:
                return candidate
        return None

    def clear_quote_references(self):
        self.quote_cache.clear()
        with self.db:
            self.db.execute("DELETE FROM voice_quote_refs WHERE scope=?", (self.service.scope,))

    async def status(self):
        try:
            r = await self.client.get("/healthz", headers=self.headers(), timeout=3)
            r.raise_for_status()
            result = r.json()
            self.ready = bool(result.get("ready"))
            return result
        except Exception:
            self.ready = False
            return {"ready": False, "reason": "service_unavailable"}

    async def prepare(self, eid, text, actor, valid, incoming=""):
        self.purge()
        if not self.preferences(actor)[0] or self.options.get("mode") == "text":
            return None
        try:
            spoken = spoken_text(text)
        except ValueError as error:
            self.last_error = str(error)
            return None
        if not valid():
            raise asyncio.CancelledError()
        delivery = plan_delivery(
            spoken, incoming, self.service.store.memory.affect(), self.delivery_factor,
            dialogue=getattr(self.service, "dialogue_context", None),
        )
        job = uuid.uuid4().hex
        stamp = time.time()
        snapshot = self.service.v2.snapshot() if self.service.v2 else {}
        with self.db:
            self.db.execute(
                "INSERT INTO voice_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job,
                    self.service.scope,
                    eid,
                    actor,
                    "running",
                    json.dumps(snapshot),
                    stamp,
                    stamp + 120,
                    None,
                    None,
                    None,
                    None,
                ),
            )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS voice_job_metadata(id TEXT PRIMARY KEY,data TEXT)"
        )
        details = {
            "canonical_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "spoken_sha256": hashlib.sha256(spoken.encode()).hexdigest(),
            "voice_id": self.options.get("profile", "dafeiyu_sweet_v1"),
            "voice_revision": self.options.get("profile_revision"),
            "memory_dependencies": (self.service.memory_packet or {}).get("items", []),
            "policy_version": "context-delivery-v1",
            "delivery": delivery,
        }
        # dependencies are identifiers/revisions only, never memory text.
        details["memory_dependencies"] = [
            {k: v for k, v in item.items() if k in {"id", "revision"}}
            for item in details["memory_dependencies"]
        ]
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO voice_job_metadata VALUES(?,?)",
                (job, json.dumps(details)),
            )
        # Text stays only in the request and transient coroutine, never the job journal.
        try:
            r = await self.client.post(
                "/v1/audio/speech",
                headers=self.headers(),
                json={
                    "model": "dafeiyu-index-tts",
                    "voice": self.options.get("profile", "dafeiyu_sweet_v1"),
                    "input": spoken,
                    "response_format": "wav",
                    "language": "ZH",
                    **delivery,
                    "request_id": job,
                },
            )
            r.raise_for_status()
            raw = r.content
            meta = validate_wav(raw, 60)
            if (
                not valid()
                or time.time() > stamp + 120
                or not self.preferences(actor)[0]
            ):
                raise asyncio.CancelledError()
            path = self.directory / (job + ".wav")
            tmp = path.with_suffix(".part")
            tmp.write_bytes(raw)
            tmp.replace(path)
            with self.db:
                self.db.execute(
                    "UPDATE voice_jobs SET state='ready',path=? WHERE id=?",
                    (str(path), job),
                )
            self.delivery_factor = delivery["duration_factor"]
            return {
                "job": job,
                "path": str(path),
                "spoken_text": spoken,
                **meta,
                "caption": bool(self.preferences(actor)[1]),
            }
        except asyncio.CancelledError:
            with self.db:
                self.db.execute(
                    "UPDATE voice_jobs SET state='cancelled' WHERE id=?", (job,)
                )
            try:
                await self.client.post(
                    "/internal/jobs/" + job + "/cancel",
                    headers=self.headers(),
                    timeout=2,
                )
            except Exception:
                pass
            self.purge()
            raise
        except Exception as error:
            self.last_error = "synthesis_" + type(error).__name__
            with self.db:
                self.db.execute(
                    "UPDATE voice_jobs SET state='failed',error=? WHERE id=?",
                    (self.last_error, job),
                )
            self.purge()
            return None

    def receipt(self, asset, kind, value):
        column = "voice_receipt" if kind == "voice" else "text_receipt"
        if value is None:
            with self.db:
                self.db.execute(
                    "UPDATE voice_jobs SET state='unknown' WHERE id=?", (asset["job"],)
                )
            return
        with self.db:
            self.db.execute(
                f"UPDATE voice_jobs SET {column}=?,state=? WHERE id=?",
                (value, "succeeded" if value else "unknown", asset["job"]),
            )

    async def close(self):
        self.invalidate()
        await self.client.aclose()
