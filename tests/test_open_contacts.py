"""Expanded routes: contact verification, peer isolation, owner and QQ favorites."""

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from tests.test_astrbot_integration import build_adapter, TOKEN
from tests.test_conversation import CONFIG, FakeModel, event, ROOT
from tests.test_group_chat import group_event, GROUP
from tests.test_vision import png
from src.stickers import StickerLibrary
from src.qq_favorites import QQFavorites

A, B, UNKNOWN = "12345001", "12345002", "12345003"
NEWGROUP = "5555555937"


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.tmp.name)
        self.model = FakeModel()
        cfg = dict(
            CONFIG,
            private_all_friends=True,
            group_all_joined=True,
            sticker_enabled=False,
            group_ids=[GROUP],
            memory_v2={"enabled": True, "mode": "active", "ui_enabled": False},
        )
        self.adapter = build_adapter(cfg, TOKEN, self.model, self.root)
        self.adapter.checked_login = AsyncMock(return_value=True)

        async def contact(action):
            return (
                [{"user_id": x} for x in [CONFIG["owner_qq"], A, B]]
                if action == "get_friend_list"
                else [{"group_id": x} for x in [GROUP, NEWGROUP]]
            )

        self.adapter.contacts.read_action = AsyncMock(side_effect=contact)
        self.adapter.bot.call_action = AsyncMock(return_value={"message_id": 55})

    async def asyncTearDown(self):
        await self.adapter.terminate()
        self.tmp.cleanup()

    def incoming(self, peer, text="你好", mid="p"):
        return event(text, mid, user_id=peer, sender={"user_id": peer})

    async def test_only_actual_friends_and_joined_groups_create_routes(self):
        self.assertIsNone(await self.adapter.target_for(self.incoming(UNKNOWN)))
        self.assertIsNone(
            await self.adapter.target_for(dict(self.incoming(A), sub_type="group"))
        )
        self.assertIsNone(await self.adapter.target_for(group_event(group="5555555939")))
        group = await self.adapter.target_for(group_event(group=NEWGROUP))
        self.assertEqual(group.group_id, NEWGROUP)
        self.assertEqual(group.config["group_random_probability"], 0)
        self.assertIs(
            await self.adapter.target_for(group_event(group=GROUP)),
            self.adapter.groups[GROUP],
        )

    async def test_two_peers_cannot_mix_memory_context_or_outbound_recipient(self):
        a = await self.adapter.target_for(self.incoming(A))
        b = await self.adapter.target_for(self.incoming(B))
        self.assertNotEqual(a.scope, b.scope)
        self.assertEqual(a.config["owner_qq"], CONFIG["owner_qq"])
        self.assertFalse(a.accepts(self.incoming(B)))
        a.v2.remember(A, "合成偏好：茉莉茶", "remember-a")
        self.assertNotIn("茉莉茶", str(b.messages("我喜欢什么茶")))
        await a.receive(self.incoming(A, "你好", "a-chat"), CONFIG["bot_qq"])
        await b.receive(self.incoming(B, "嗨", "b-chat"), CONFIG["bot_qq"])
        await asyncio.wait_for(a.queue.join(), 2)
        await asyncio.wait_for(b.queue.join(), 2)
        recipients = [
            c.kwargs["user_id"]
            for c in self.adapter.bot.call_action.await_args_list
            if c.args[0] == "send_private_msg"
        ]
        self.assertEqual(set(recipients), {int(A), int(B)})
        self.assertEqual(a.store.budget_scope, self.adapter.service.scope)
        self.assertNotIn(a.scope, self.adapter.memory_ui_services)
        self.assertFalse(a.v2.list_memories(B))

    async def test_personal_optout_is_local_and_diagnostics_remain_owner_only(self):
        a = await self.adapter.target_for(self.incoming(A))
        a.v2.remember(A, "合成偏好：淡茶", "remember")
        await a.receive(self.incoming(A, "大肥鱼 不要记我", "optout"), CONFIG["bot_qq"])
        self.assertFalse(a.v2.policy(A)["enabled"])
        self.assertTrue(self.adapter.service.v2.policy(CONFIG["owner_qq"])["enabled"])
        await a.receive(
            self.incoming(A, "大肥鱼 记忆整理状态", "admin"), CONFIG["bot_qq"]
        )
        self.assertIn(
            "只对机器人主人",
            str(self.adapter.bot.call_action.await_args_list[-1].kwargs["message"]),
        )
        await a.receive(
            self.incoming(A, "大肥鱼 检查资料接口", "profile-admin"), CONFIG["bot_qq"]
        )
        self.assertIn(
            "主人操作",
            str(self.adapter.bot.call_action.await_args_list[-1].kwargs["message"]),
        )

    async def test_browser_artifact_is_sent_only_to_bound_peer_after_file_hash_check(
        self,
    ):
        await self.adapter.target_for(self.incoming(A))
        directory = self.root / "runtime/browser/artifacts/synthetic"
        directory.mkdir(parents=True)
        path = directory / "screenshot.jpg"
        path.write_bytes(png())
        artifact = {
            "id": "a-synthetic",
            "path": str(path),
            "kind": "image",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        await self.adapter.send_browser_artifact(artifact, peer_qq=A)
        self.assertEqual(
            self.adapter.bot.call_action.await_args.args, ("send_private_msg",)
        )
        self.assertEqual(
            self.adapter.bot.call_action.await_args.kwargs["user_id"], int(A)
        )
        path.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            await self.adapter.send_browser_artifact(artifact, peer_qq=A)
        foreign = self.root / "outside.jpg"
        foreign.write_bytes(png())
        artifact.update(
            path=str(foreign), sha256=hashlib.sha256(foreign.read_bytes()).hexdigest()
        )
        with self.assertRaises(ValueError):
            await self.adapter.send_browser_artifact(artifact, peer_qq=A)

    async def test_removed_contact_cannot_receive_pending_reply(self):
        await self.adapter.target_for(self.incoming(A))
        self.adapter.contacts.friends.discard(A)
        with self.assertRaises(ConnectionError):
            await self.adapter.send_private_text(A, "未送出的合成消息")
        self.adapter.bot.call_action.assert_not_awaited()


class FavoriteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.folder = Path(self.tmp.name) / "stickers"
        self.bot = CONFIG["bot_qq"]
        self.library = StickerLibrary(
            self.folder, account_scope=self.bot, downloader=AsyncMock()
        )

    async def asyncTearDown(self):
        await self.library.close()
        self.tmp.cleanup()

    async def test_account_add_once_and_verify_from_qq_list(self):
        raw = png()
        token = self.library.add_bytes(self.bot, raw, "合成开心")
        md5 = hashlib.md5(raw).hexdigest()
        native = AsyncMock(return_value={"result": 0})
        favorites = QQFavorites(self.library, native, self.bot)
        await favorites.ensure_saved(token)
        await favorites.ensure_saved(token)
        native.assert_awaited_once()
        self.assertEqual(native.await_args.args, ("add_custom_face",))
        self.assertTrue(Path(native.await_args.kwargs["file"]).is_file())
        native.reset_mock()
        native.return_value = [
            {"md5": md5, "url": "https://gchat.qpic.cn/synthetic.png"}
        ]
        await favorites.sync()
        self.assertEqual(favorites.status()["states"], {"synced": 1})
        self.library.downloader.download_raw.assert_not_awaited()
        await favorites.close()

    async def test_unknown_outcome_never_blindly_repeats_add(self):
        token = self.library.add_bytes(self.bot, png(), "合成")
        native = AsyncMock(side_effect=TimeoutError)
        favorites = QQFavorites(self.library, native, self.bot)
        await favorites.ensure_saved(token)
        await favorites.ensure_saved(token)
        native.assert_awaited_once()
        self.assertEqual(favorites.status()["states"], {"adding_unknown": 1})
        await favorites.close()

    async def test_old_group_cache_migrates_to_account_and_deleted_entry_does_not_resurrect(
        self,
    ):
        await self.library.close()
        old = StickerLibrary(self.folder)
        token = old.add_bytes(GROUP, png(), "旧群合成")
        await old.close()
        self.library = StickerLibrary(self.folder, account_scope=self.bot)
        self.assertEqual(self.library.catalog([GROUP]), [])
        self.assertEqual(self.library.catalog([self.bot])[0]["id"], token)
        self.library.delete(token, [self.bot])
        await self.library.close()
        self.library = StickerLibrary(self.folder, account_scope=self.bot)
        self.assertEqual(self.library.catalog([self.bot]), [])
