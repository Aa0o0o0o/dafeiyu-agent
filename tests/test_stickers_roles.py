"""Synthetic sticker files, group context and master/admin/owner regression."""

import asyncio
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from PIL import Image

from src.contracts import ModelResult
from src.stickers import (
    StickerLibrary,
    sticker_sources,
    take_sticker_marker,
    backup_library,
)
from tests import test_group_chat as groups
from tests.test_vision import png, URL


def sticker_part(url=URL):
    return {"type": "image", "data": {"url": url, "sub_type": 1, "summary": "开心"}}


class LibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=groups.ROOT / ".tmp")
        self.loader = AsyncMock()
        self.loader.download_raw.return_value = png()
        self.lib = StickerLibrary(
            Path(self.tmp.name) / "library", downloader=self.loader
        )

    async def asyncTearDown(self):
        if not self.lib.closed:
            await self.lib.close()
        self.tmp.cleanup()

    async def test_only_explicit_stickers_are_collected_and_duplicates_download_once(
        self,
    ):
        normal = {"type": "image", "data": {"url": URL}}
        self.assertEqual(sticker_sources([normal]), [])
        self.lib.enqueue(groups.GROUP, [normal, sticker_part(), sticker_part()])
        await asyncio.wait_for(self.lib.queue.join(), 2)
        self.lib.enqueue(groups.GROUP, [sticker_part()])
        self.loader.download_raw.assert_awaited_once_with(URL)
        self.assertEqual(self.lib.stats([groups.GROUP])[0], 1)

    async def test_raw_gif_and_dimensions_are_preserved_and_bytes_deduped(self):
        a = Image.new("RGB", (50, 40), "red")
        b = Image.new("RGB", (50, 40), "blue")
        out = io.BytesIO()
        a.save(
            out, format="GIF", save_all=True, append_images=[b], duration=100, loop=0
        )
        raw = out.getvalue()
        token = self.lib.add_bytes(groups.GROUP, raw, "俏皮")
        self.assertEqual(token, self.lib.add_bytes(groups.GROUP, raw, "重复"))
        path, _ = self.lib.resolve(token, [groups.GROUP])
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(
            hashlib.sha256(path.read_bytes()).hexdigest(),
            hashlib.sha256(raw).hexdigest(),
        )
        with Image.open(path) as im:
            self.assertEqual(im.n_frames, 2)
            self.assertEqual(im.size, (50, 40))

    async def test_scope_capacity_and_integrity_are_enforced(self):
        token = self.lib.add_bytes(groups.GROUP, png(), "笑")
        self.assertEqual(self.lib.catalog([groups.OTHER]), [])
        with self.assertRaises(ValueError):
            self.lib.resolve(token, [groups.OTHER])
        self.lib.max_items = 1
        out = io.BytesIO()
        Image.new("RGB", (16, 16), "green").save(out, format="PNG")
        with self.assertRaises(ValueError):
            self.lib.add_bytes(groups.GROUP, out.getvalue(), "绿")
        path, _ = self.lib.resolve(token, [groups.GROUP])
        path.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.lib.resolve(token, [groups.GROUP])

    async def test_path_tampering_cannot_select_an_outside_file(self):
        token = self.lib.add_bytes(groups.GROUP, png(), "图")
        outside = Path(self.tmp.name) / "outside.png"
        outside.write_bytes(png())
        with self.lib.db:
            self.lib.db.execute("UPDATE stickers SET filename=?", (str(outside),))
        with self.assertRaises(ValueError):
            self.lib.resolve(token, [groups.GROUP])

    async def test_owner_labels_delete_and_restart_keep_only_intended_assets(self):
        token = self.lib.add_bytes(groups.GROUP, png(), "笑")
        self.lib.rename(token, [groups.GROUP], "开心 好耶")
        await self.lib.close()
        self.lib = StickerLibrary(
            Path(self.tmp.name) / "library", downloader=self.loader
        )
        self.assertEqual(
            self.lib.catalog([groups.GROUP])[0]["description"], "开心 好耶"
        )
        path, _ = self.lib.resolve(token, [groups.GROUP])
        self.lib.delete(token, [groups.GROUP])
        self.assertFalse(path.exists())
        self.assertEqual(self.lib.stats([groups.GROUP])[0], 0)

    async def test_bulk_collection_has_no_per_minute_quota(self):
        for i in range(8):
            self.lib.enqueue(groups.GROUP, [sticker_part(URL + f"&n={i}")])
        await asyncio.wait_for(self.lib.queue.join(), 2)
        self.assertEqual(self.loader.download_raw.await_count, 8)

    async def test_sticker_marker_is_never_executed_inside_code(self):
        token = "S" + "a" * 16
        self.assertEqual(
            take_sticker_marker("好耶\n[[sticker:" + token + "]]"), ("好耶", token)
        )
        text = "```\n[[sticker:" + token + "]]"
        self.assertEqual(take_sticker_marker(text), (text, None))
        self.assertEqual(take_sticker_marker("好\n[[sticker:../../x]]"), ("好", None))

    async def test_backup_preserves_index_and_raw_assets_and_reports_missing(self):
        token = self.lib.add_bytes(groups.GROUP, png(), "开心")
        out = Path(self.tmp.name) / "backup"
        result = backup_library(self.lib.root, out)
        self.assertEqual(result, {"complete": True, "files": 1, "missing": 0})
        original, _ = self.lib.resolve(token, [groups.GROUP])
        self.assertEqual(
            (out / original.relative_to(self.lib.root)).read_bytes(), png()
        )
        original.unlink()
        partial = backup_library(self.lib.root, Path(self.tmp.name) / "partial")
        self.assertEqual(partial["missing"], 1)
        self.assertFalse(partial["complete"])


class GroupRefreshRoleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = groups.GroupTests.asyncSetUp
    asyncTearDown = groups.GroupTests.asyncTearDown
    receive = groups.GroupTests.receive

    async def test_new_group_message_during_settle_is_read_before_answer(self):
        self.group.config["group_context_settle_seconds"] = 1

        async def settle(_):
            await self.group.receive(
                groups.group_event(
                    "补充：刚改成周五了", mid="after-at", mentioned=False
                ),
                groups.CONFIG["bot_qq"],
            )

        self.group.sleep = AsyncMock(side_effect=settle)
        await self.receive(groups.group_event("什么时候开会", mid="ask"))
        self.assertIn("刚改成周五", str(self.model.calls[0]))
        self.assertEqual(len(self.model.calls), 1)

    async def test_bot_master_admin_and_real_group_owner_are_distinct(self):
        owner = groups.group_event(
            "我是群主", mid="group-owner", member="23456789", mentioned=False
        )
        owner["sender"]["role"] = "owner"
        await self.receive(owner)
        master = groups.group_event(
            "我是机器人主人", mid="master", member=groups.CONFIG["owner_qq"]
        )
        master["sender"]["role"] = "admin"
        await self.receive(master)
        packet = next(
            m["content"]
            for m in self.model.calls[0]
            if "<group_context_data>" in m["content"]
        )
        self.assertIn('"speaker_is_bot_master": true', packet)
        self.assertIn('"speaker_group_role": "admin"', packet)
        self.assertIn('"group_owner_qq": "23456789"', packet)
        self.assertNotIn('"speaker_is_owner"', packet)

    async def test_actual_qq_role_updates_a_user_hint_without_changing_bot_permissions(
        self,
    ):
        self.group.group_roles[groups.CONFIG["owner_qq"]] = "admin"
        actual = groups.group_event(
            "你好", mid="changed-role", member=groups.CONFIG["owner_qq"]
        )
        actual["sender"]["role"] = "member"
        await self.receive(actual)
        self.assertEqual(self.group.group_roles[groups.CONFIG["owner_qq"]], "member")
        group_owner = groups.group_event(
            "大肥鱼 新对话", mid="unauthorized", member="23456789"
        )
        group_owner["sender"]["role"] = "owner"
        await self.receive(group_owner)
        self.assertIn("主人", self.group_channel.sent[-1])


class StickerReplyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = groups.GroupTests.asyncSetUp
    asyncTearDown = groups.GroupTests.asyncTearDown
    receive = groups.GroupTests.receive

    async def test_sticker_uses_same_model_turn_and_never_leaks_marker_or_path(self):
        lib = StickerLibrary(Path(self.tmp.name) / "stickers")
        self.group.sticker_library = lib
        token = lib.add_bytes(groups.GROUP, png(), "开心")
        self.group_channel.send_sticker = AsyncMock(return_value="sticker-receipt")
        self.model.complete = AsyncMock(
            return_value=ModelResult("好耶\n[[sticker:" + token + "]]")
        )
        try:
            await self.receive(groups.group_event("来个开心的表情", mid="sticker1"))
            self.assertEqual(self.model.complete.await_count, 1)
            self.assertEqual(self.group_channel.sent, ["好耶"])
            self.group_channel.send_sticker.assert_awaited_once_with(token)
            self.assertNotIn("[[sticker:", str(self.group.recent))
            await self.receive(groups.group_event("继续聊", mid="sticker2"))
            self.assertEqual(self.group_channel.send_sticker.await_count, 2)
        finally:
            await lib.close()

    async def test_unknown_sticker_cannot_be_sent(self):
        lib = StickerLibrary(Path(self.tmp.name) / "stickers")
        self.group.sticker_library = lib
        self.group_channel.send_sticker = AsyncMock()
        self.model.complete = AsyncMock(
            return_value=ModelResult("好\n[[sticker:S" + "a" * 16 + "]]")
        )
        try:
            await self.receive(groups.group_event("聊聊", mid="unknown"))
            self.group_channel.send_sticker.assert_not_awaited()
            self.assertEqual(self.group_channel.sent, ["好"])
        finally:
            await lib.close()
