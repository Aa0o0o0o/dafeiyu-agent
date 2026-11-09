"""Mirror real bot-account QQ favorites; only verified local stickers may be added."""

import asyncio
import hashlib
import time
import re
from urllib.parse import urlsplit
from .vision import QQ_IMAGE_HOSTS


class QQFavorites:
    def __init__(self, library, action, bot_qq):
        self.library, self.action, self.bot_qq = library, action, bot_qq
        library.db.execute(
            "CREATE TABLE IF NOT EXISTS qq_favorites(token TEXT PRIMARY KEY,md5 TEXT NOT NULL,state TEXT NOT NULL,updated REAL NOT NULL)"
        )
        library.db.commit()
        library.on_collect = self.ensure_saved
        self.task = None
        self.lock = asyncio.Lock()
        self.last_error = None
        self.last_sync = 0

    def mark(self, token, md5, state):
        with self.library.db:
            self.library.db.execute(
                "INSERT OR REPLACE INTO qq_favorites VALUES(?,?,?,?)",
                (token, md5, state, time.time()),
            )

    async def ensure_saved(self, token):
        async with self.lock:
            row = self.library.db.execute(
                "SELECT state FROM qq_favorites WHERE token=?", (token,)
            ).fetchone()
            if (
                row
            ):  # Unknown outcomes must be reconciled by reading, never blindly retried.
                return
            path, _ = self.library.resolve(token, [self.bot_qq])
            md5 = hashlib.md5(path.read_bytes()).hexdigest()
            self.mark(token, md5, "adding_unknown")
            try:
                result = await self.action("add_custom_face", file=str(path))
                if isinstance(result, dict) and result.get(
                    "result", result.get("errCode", 0)
                ) not in [0, None]:
                    self.mark(token, md5, "rejected")
                    self.last_error = "QQ未接受此表情收藏；没有反复重试"
                    return
                self.mark(token, md5, "awaiting_verification")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = "QQ收藏结果未确认：" + type(error).__name__

    async def remote_items(self):
        count = 100
        while True:
            rows = await self.action("fetch_custom_face_detail", count=count)
            if not isinstance(rows, list):
                raise ValueError("Invalid QQ favorite response")
            if len(rows) < count or count >= 6400:
                return rows
            count *= 2

    async def sync(self):
        try:
            remote = await self.remote_items()
            remote_hashes = set()
            for item in remote:
                if not isinstance(item, dict):
                    continue
                md5 = str(item.get("md5", "")).lower()
                if re.fullmatch("[0-9a-f]{32}", md5):
                    remote_hashes.add(md5)
                    local = self.library.db.execute(
                        "SELECT token FROM qq_favorites WHERE md5=?", (md5,)
                    ).fetchone()
                    if local:
                        self.mark(local[0], md5, "synced")
                        continue
                url = item.get("url")
                if not isinstance(url, str) or len(url) > 4096:
                    continue
                # Remote QQ paths never become local arbitrary paths or non-Tencent downloads.
                host = urlsplit(url).hostname or ""
                if host not in QQ_IMAGE_HOSTS:
                    continue
                if url.startswith("http://"):
                    url = "https://" + url[7:]
                try:
                    raw = await self.library.downloader.download_raw(url)
                    actual = hashlib.md5(raw).hexdigest()
                    token = self.library.add_bytes(
                        self.bot_qq, raw, item.get("desc") or "QQ账号收藏表情"
                    )
                    remote_hashes.add(actual)
                    self.mark(token, actual, "synced")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.last_error = "部分QQ收藏未能缓存；原账号收藏保持"
            for (token,) in self.library.db.execute(
                "SELECT id FROM stickers WHERE group_id=?", (self.bot_qq,)
            ).fetchall():
                path, _ = self.library.resolve(token, [self.bot_qq])
                md5 = hashlib.md5(path.read_bytes()).hexdigest()
                if md5 in remote_hashes:
                    self.mark(token, md5, "synced")
                else:
                    await self.ensure_saved(token)
            self.last_sync = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = "QQ账号收藏同步未完成：" + type(error).__name__

    def schedule(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.sync())

    def status(self):
        return {
            "states": dict(
                self.library.db.execute(
                    "SELECT state,COUNT(*) FROM qq_favorites GROUP BY state"
                ).fetchall()
            ),
            "last_sync": self.last_sync,
            "last_error": self.last_error,
        }

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
