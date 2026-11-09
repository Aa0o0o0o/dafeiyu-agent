"""Per-group bounded sticker library; raw GIFs retained, no model-side file access."""

import asyncio
import hashlib
import io
import json
import re
import sqlite3
import time
from pathlib import Path
from contextlib import closing

from PIL import Image

from .memory_state import safe_text
from .message_surface import strip_delivery_annotations
from .sticker_memory import StickerMemory
from .vision import QQImageLoader, ImageInputError, image_data_url, parse_message


def checked_market_face(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid market face")
    package = str(data.get("emoji_package_id", ""))
    emoji = data.get("emoji_id", "")
    key = data.get("key", "")
    if (
        not package.isdigit()
        or not 0 < int(package) < 2**31
        or not isinstance(emoji, str)
        or not re.fullmatch(r"[a-fA-F0-9]{16,64}", emoji)
        or not isinstance(key, str)
        or not re.fullmatch(r"[a-fA-F0-9]{16,128}", key)
    ):
        raise ValueError("Incomplete market face replay metadata")
    summary = data.get("summary", "[商城表情]")
    if not isinstance(summary, str) or len(summary) > 60 or safe_text(summary) is None:
        summary = "[商城表情]"
    return {
        "emoji_package_id": int(package),
        "emoji_id": emoji,
        "key": key,
        "summary": summary,
    }


def sticker_sources(parts):
    found = []
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, dict) or not isinstance(part.get("data"), dict):
            continue
        data = part["data"]
        if part.get("type") != "mface" and not (
            part.get("type") == "image" and data.get("sub_type") in (1, "1")
        ):
            continue
        try:
            parsed = parse_message([part])
        except ImageInputError:
            continue
        label = data.get("summary")
        label = label[:60].strip() if isinstance(label, str) else ""
        label = label if label and safe_text(label) else "未标注语义的群友表情"
        for url in parsed.images:
            found.append((url, label))
    return found


def take_sticker_marker(text):
    # Control is allowed only on its own final line, outside an open code fence.
    match = re.search(r"(?m)^[ \t]*\[\[sticker:([^\]\r\n]{1,80})\]\][ \t]*\s*\Z", text)
    if not match or text[: match.start()].count("```") % 2:
        return text, None
    token = match[1]
    return text[: match.start()].rstrip(), token if re.fullmatch(
        r"S[0-9a-f]{12,64}", token
    ) else None


def sticker_reply_parts(text, allowed, faces=None):
    """Parse inline and standalone media without executing code or leaking controls."""
    text = strip_delivery_annotations(text)
    parts, pending = [], []
    fence, ticks = None, 0
    markers = re.compile(
        r"`+|\[(\[?)(?:(sticker|face)\s*:\s*([^\]\r\n]*)(\]\]?|$)|(dice|rps)(\]\]?|$))",
        re.I,
    )

    def flush():
        value = "".join(pending).strip()
        if value:
            parts.append(("text", value))
        pending.clear()

    for line in text.splitlines(keepends=True):
        boundary = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            pending.append(line)
            if boundary and boundary[1][0] == fence[0] and len(boundary[1]) >= fence[1]:
                fence = None
            continue
        if boundary and not ticks:
            fence = (boundary[1][0], len(boundary[1]))
            pending.append(line)
            continue
        pos = 0
        for match in markers.finditer(line):
            pending.append(line[pos : match.start()])
            token = match[0]
            if token.startswith("`"):
                if ticks == len(token):
                    ticks = 0
                elif not ticks:
                    ticks = len(token)
                pending.append(token)
            elif ticks:
                pending.append(token)
            else:
                flush()
                kind = (match[2] or match[5]).lower()
                value = (match[3] or "").strip()
                closing = match[4] or match[6]
                complete = closing == ("]]" if match[1] else "]")
                if complete:
                    if kind == "sticker" and value in allowed:
                        parts.append((kind, value))
                    elif kind == "face" and faces is not None and value in faces:
                        parts.append((kind, value))
                    elif kind in {"dice", "rps"} and faces is not None:
                        parts.append((kind, ""))
                # Invalid/disabled controls are removed; never fabricate a valid ID.
            pos = match.end()
        pending.append(line[pos:])
    flush()
    return parts


def backup_library(source_root, destination):
    """Consistent index plus media bytes; explicitly report concurrent deletions."""
    source_root, destination = Path(source_root).resolve(), Path(destination).resolve()
    index = source_root / "index.sqlite3"
    if not index.exists():
        return {"complete": True, "files": 0, "missing": 0}
    destination.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(destination / "index.sqlite3")) as snapshot,
    ):
        source.backup(snapshot)
        rows = snapshot.execute("SELECT filename,digest FROM stickers").fetchall()
    copied, missing = 0, 0
    for name, digest in rows:
        original, target = source_root / name, destination / name
        if (
            not original.resolve().is_relative_to(source_root)
            or not target.resolve().is_relative_to(destination)
            or original.is_symlink()
        ):
            raise ValueError("Sticker backup path outside library")
        try:
            raw = original.read_bytes()
        except FileNotFoundError:
            missing += 1
            continue
        if hashlib.sha256(raw).hexdigest() != digest:
            missing += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        copied += 1
    return {"complete": missing == 0, "files": copied, "missing": missing}


class StickerLibrary:
    def __init__(
        self,
        root,
        *,
        downloader=None,
        max_items=None,
        max_bytes=None,
        account_scope=None,
        memory_options=None,
    ):
        self.root = Path(root).resolve()
        if self.root != Path(root).absolute():
            raise ValueError("Sticker root must not traverse a link")
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "index.sqlite3")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS stickers(group_id TEXT,id TEXT,digest TEXT,filename TEXT,label TEXT,bytes INTEGER,created REAL,last_used REAL,PRIMARY KEY(group_id,id),UNIQUE(group_id,digest))"
        )
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sticker_meanings(
                group_id TEXT,token TEXT,digest TEXT,description TEXT,visible_text TEXT,
                tags TEXT,emotions TEXT,updated REAL,PRIMARY KEY(group_id,token));
            CREATE TABLE IF NOT EXISTS sticker_native(
                group_id TEXT,token TEXT,digest TEXT,data TEXT,PRIMARY KEY(group_id,token));
        """)
        columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(sticker_meanings)")
        }
        if "source" not in columns:
            self.db.execute(
                "ALTER TABLE sticker_meanings ADD COLUMN source TEXT NOT NULL DEFAULT 'model_visual'"
            )
        self.db.commit()
        if account_scope is not None and not re.fullmatch(
            r"[1-9][0-9]{4,19}", account_scope
        ):
            raise ValueError("Invalid bot account sticker scope")
        self.account_scope, self.on_collect = account_scope, None
        if account_scope:
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO stickers SELECT ?,id,digest,filename,label,bytes,created,last_used FROM stickers WHERE group_id!=?",
                    (account_scope, account_scope),
                )
        if account_scope:
            with self.db:
                self.db.execute(
                    "DELETE FROM stickers WHERE group_id!=?", (account_scope,)
                )
        self.downloader = downloader or QQImageLoader()
        self.max_items, self.max_bytes = max_items, max_bytes
        self.queue = asyncio.Queue(maxsize=8)
        self.worker = None
        self.pending = set()
        self.attempted = {}
        self.last_error = None
        self.closed = False
        self.memory = StickerMemory(self.db, memory_options)

    def enqueue(self, group_id, parts):
        if self.closed:
            return
        group_id = self.account_scope or group_id
        for url, label in sticker_sources(parts):
            key = (group_id, url)
            self.attempted = {
                k: t for k, t in self.attempted.items() if time.monotonic() - t < 300
            }
            if key in self.pending or key in self.attempted or self.queue.full():
                continue
            self.pending.add(key)
            self.attempted[key] = time.monotonic()
            native = None
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "mface":
                    try:
                        if url in parse_message([part]).images:
                            native = checked_market_face(part.get("data", {}))
                    except (ValueError, ImageInputError):
                        pass
            self.queue.put_nowait((group_id, url, label, native))
        if not self.queue.empty() and (self.worker is None or self.worker.done()):
            self.worker = asyncio.create_task(self.run())

    async def run(self):
        while not self.queue.empty() and not self.closed:
            group, url, label, native = self.queue.get_nowait()
            try:
                count, size = self.stats([group])
                if (self.max_items is not None and count >= self.max_items) or (
                    self.max_bytes is not None and size >= self.max_bytes
                ):
                    self.last_error = "表情库容量已满，请主人删除不需要的收藏"
                    continue
                raw = await self.downloader.download_raw(url)
                token = self.add_bytes(group, raw, label)
                if native:
                    self.remember_native(token, [group], native)
                if self.on_collect:
                    await self.on_collect(token)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = "收藏未完成：" + type(error).__name__
            finally:
                self.pending.discard((group, url))
                self.queue.task_done()

    def stats(self, groups):
        if not groups:
            return 0, 0
        q = ",".join("?" for _ in groups)
        return self.db.execute(
            f"SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM stickers WHERE group_id IN ({q})",
            tuple(groups),
        ).fetchone()

    def add_bytes(self, group, raw, label):
        if not re.fullmatch(r"[1-9][0-9]{4,19}", group):
            raise ValueError("Invalid sticker scope")
        image_data_url(raw)  # Strict existing format/pixel/decode/8MiB boundary.
        with Image.open(io.BytesIO(raw)) as image:
            if (
                getattr(image, "n_frames", 1) > 300
                or getattr(image, "n_frames", 1) * image.width * image.height
                > 100_000_000
            ):
                raise ImageInputError("表情动图展开尺寸过大")
            suffix = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp"}[
                image.format
            ]
        digest = hashlib.sha256(raw).hexdigest()
        old = self.db.execute(
            "SELECT id FROM stickers WHERE group_id=? AND digest=?", (group, digest)
        ).fetchone()
        if old:
            self.memory.touch(group, old[0])
            return old[0]
        count, size = self.stats([group])
        if (self.max_items is not None and count >= self.max_items) or (
            self.max_bytes is not None and size + len(raw) > self.max_bytes
        ):
            raise ValueError("Sticker capacity reached")
        token = "S" + digest[:16]
        name = f"{group}/{digest}.{suffix}"
        path = self.root / name
        path.parent.mkdir(exist_ok=True)
        # Reparse directories are never used for persistent media or sending.
        if path.parent.resolve() != self.root / group:
            raise ValueError("Sticker directory escaped scope")
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(path)
        with self.db:
            self.db.execute(
                "INSERT INTO stickers VALUES(?,?,?,?,?,?,?,0)",
                (
                    group,
                    token,
                    digest,
                    name,
                    label[:60] if safe_text(label) else "未标注语义的群友表情",
                    len(raw),
                    time.time(),
                ),
            )
        self.memory.touch(group, token)
        return token

    def catalog(self, groups, query="", limit=16, *, include_archived=False):
        if not groups:
            return []
        self.memory.maintain()
        q = ",".join("?" for _ in groups)
        rows = self.db.execute(
            f"""SELECT s.id,s.label,s.created,s.last_used,m.description,m.visible_text,
                m.tags,m.emotions,m.source,COALESCE(a.last_seen,s.created),COALESCE(a.uses,0),a.archived
                FROM stickers s LEFT JOIN sticker_meanings m
                ON m.group_id=s.group_id AND m.token=s.id AND m.digest=s.digest
                LEFT JOIN sticker_activity a ON a.group_id=s.group_id AND a.token=s.id
                WHERE s.group_id IN ({q}) {"" if include_archived else "AND a.archived IS NULL"}
                ORDER BY s.created DESC""",
            tuple(groups),
        ).fetchall()
        terms = {query[i : i + 2] for i in range(max(0, len(query) - 1))}
        # Lexical hints recall candidates; only the chat model decides whether to send.
        for aliases, expansion in (
            (("哈哈", "好笑", "乐", "开心", "笑死"), "开心 欢乐 大笑"),
            (("无语", "绷", "离谱", "抽象", "服了"), "无奈 吐槽 嫌弃"),
            (("难过", "累", "伤心", "安慰"), "安慰 拥抱 委屈"),
            (("谢谢", "感谢", "厉害", "成功"), "感谢 赞同 欢乐"),
            (("怎么", "为什么", "什么", "问号"), "疑惑 惊讶"),
            (("早安", "晚安", "拜拜", "你好"), "问候 告别 困倦"),
        ):
            if any(word in query for word in aliases):
                terms.update(expansion.split())

        def score(row):
            material = " ".join(str(x or "") for x in (row[1], *row[4:9]))
            return (
                sum(term in material for term in terms),
                self.memory.strength(max(row[2], row[3], row[9]), row[10]),
                -row[3],
            )

        rows.sort(key=score, reverse=True)
        result, seen = [], set()
        for (
            token,
            label,
            _,
            _,
            description,
            visible,
            tags,
            emotions,
            source,
            seen_at,
            uses,
            archived,
        ) in rows:
            if token in seen:
                continue
            item = {
                "id": token,
                "description": description or label,
                "qq_summary": label,
                "meaning_verified": bool(description),
                "meaning_source": source if description else "qq_or_owner_label",
                "memory_state": "archived" if archived is not None else "active",
            }
            if description:
                item.update(
                    visible_text=visible,
                    tags=json.loads(tags),
                    emotions=json.loads(emotions),
                )
            result.append(item)
            seen.add(token)
            if len(result) >= limit:
                break
        return result

    def remember_meaning(
        self, token, groups, *, description, visible_text, tags, emotions
    ):
        path, _ = self.resolve(token, groups)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if (
            not isinstance(description, str)
            or not 1 <= len(description) <= 240
            or not isinstance(visible_text, str)
            or len(visible_text) > 160
            or any(safe_text(value) is None for value in (description, visible_text))
        ):
            raise ValueError("Invalid image-only description")
        for items, maximum in ((tags, 24), (emotions, 12)):
            if (
                not isinstance(items, list)
                or len(items) > 8
                or any(
                    not isinstance(x, str)
                    or not 1 <= len(x) <= maximum
                    or safe_text(x) is None
                    for x in items
                )
            ):
                raise ValueError("Invalid expression tags")
        # Only the current authorized collection receives the derivative cache.
        q = ",".join("?" for _ in groups)
        rows = self.db.execute(
            f"SELECT group_id FROM stickers WHERE id=? AND group_id IN ({q})",
            (token, *groups),
        ).fetchall()
        with self.db:
            for (group,) in rows:
                previous = self.db.execute(
                    "SELECT source FROM sticker_meanings WHERE group_id=? AND token=?",
                    (group, token),
                ).fetchone()
                if previous and previous[0] == "owner_label":
                    continue
                self.db.execute(
                    "INSERT OR REPLACE INTO sticker_meanings(group_id,token,digest,description,visible_text,tags,emotions,updated) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        group,
                        token,
                        digest,
                        description,
                        visible_text,
                        json.dumps(tags, ensure_ascii=False),
                        json.dumps(emotions, ensure_ascii=False),
                        time.time(),
                    ),
                )

    def remember_native(self, token, groups, data):
        path, _ = self.resolve(token, groups)
        data = checked_market_face(data)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        q = ",".join("?" for _ in groups)
        rows = self.db.execute(
            f"SELECT group_id FROM stickers WHERE id=? AND group_id IN ({q})",
            (token, *groups),
        ).fetchall()
        with self.db:
            for (group,) in rows:
                self.db.execute(
                    "INSERT OR REPLACE INTO sticker_native VALUES(?,?,?,?)",
                    (group, token, digest, json.dumps(data, ensure_ascii=False)),
                )

    def native_segment(self, token, groups):
        path, _ = self.resolve(token, groups)
        q = ",".join("?" for _ in groups)
        row = self.db.execute(
            f"SELECT digest,data FROM sticker_native WHERE token=? AND group_id IN ({q})",
            (token, *groups),
        ).fetchone()
        if row and row[0] == hashlib.sha256(path.read_bytes()).hexdigest():
            return {"type": "mface", "data": checked_market_face(json.loads(row[1]))}
        return None

    def resolve(self, token, groups):
        if not re.fullmatch(r"S[0-9a-f]{12,64}", str(token)) or not groups:
            raise ValueError("Unknown sticker")
        q = ",".join("?" for _ in groups)
        row = self.db.execute(
            f"SELECT filename,digest,label FROM stickers WHERE id=? AND group_id IN ({q}) LIMIT 1",
            (token, *groups),
        ).fetchone()
        if not row:
            raise ValueError("Sticker outside current collection")
        path = self.root / row[0]
        if (
            not path.is_file()
            or not path.resolve().is_relative_to(self.root)
            or path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != row[1]
        ):
            raise ValueError("Sticker integrity or path invalid")
        return path, row[2]

    def mark_used(self, token, groups):
        q = ",".join("?" for _ in groups)
        with self.db:
            self.db.execute(
                f"UPDATE stickers SET last_used=? WHERE id=? AND group_id IN ({q})",
                (time.time(), token, *groups),
            )

        for group in groups:
            if self.db.execute(
                "SELECT 1 FROM stickers WHERE group_id=? AND id=?", (group, token)
            ).fetchone():
                self.memory.touch(group, token, used=True)

    def rename(self, token, groups, label):
        self.resolve(token, groups)
        if not 1 <= len(label) <= 60 or safe_text(label) is None:
            raise ValueError("Invalid sticker label")
        q = ",".join("?" for _ in groups)
        with self.db:
            self.db.execute(
                f"UPDATE stickers SET label=? WHERE id=? AND group_id IN ({q})",
                (label, token, *groups),
            )
            digest = hashlib.sha256(
                self.resolve(token, groups)[0].read_bytes()
            ).hexdigest()
            for (group,) in self.db.execute(
                f"SELECT group_id FROM stickers WHERE id=? AND group_id IN ({q})",
                (token, *groups),
            ).fetchall():
                self.db.execute(
                    "INSERT OR REPLACE INTO sticker_meanings VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        group,
                        token,
                        digest,
                        label,
                        "",
                        "[]",
                        "[]",
                        time.time(),
                        "owner_label",
                    ),
                )

        self.memory.last_maintenance = 0

    def delete(self, token, groups):
        path, _ = self.resolve(token, groups)
        # Scope lookup and resolved absolute containment checked before removal.
        rows = self.db.execute(
            "SELECT group_id,filename FROM stickers WHERE id=?", (token,)
        ).fetchall()
        with self.db:
            for group, name in rows:
                if group in groups:
                    target = self.root / name
                    if (
                        not target.resolve().is_relative_to(self.root)
                        or target.is_symlink()
                    ):
                        raise ValueError("Invalid sticker delete target")
                    target.unlink(missing_ok=True)
                    self.db.execute(
                        "DELETE FROM stickers WHERE id=? AND group_id=?", (token, group)
                    )
                    self.db.execute(
                        "DELETE FROM sticker_meanings WHERE token=? AND group_id=?",
                        (token, group),
                    )
                    self.db.execute(
                        "DELETE FROM sticker_native WHERE token=? AND group_id=?",
                        (token, group),
                    )
                    self.db.execute(
                        "DELETE FROM sticker_activity WHERE token=? AND group_id=?",
                        (token, group),
                    )

    async def close(self):
        self.closed = True
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        self.db.close()
