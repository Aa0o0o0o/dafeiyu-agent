import json
import time
import unittest
from unittest.mock import AsyncMock

from src.contracts import ModelResult
from src.wechat_private_store import AccessDenied
from tests.test_wechat_private_store import WeChatPrivateStoreTests, AUTH
from tests.test_wechat_private_context import OwnerContextTests
from tests.test_group_chat import GroupTests
from tests.test_autonomy import AutonomyTests


class TimelineTests(WeChatPrivateStoreTests):
    def test_pages_cover_long_text_without_loss_and_guard_access(self):
        epoch = self.enable()
        original = '这是一条合成长消息。' * 400
        self.store.import_batch(**AUTH, batch_id='timeline', snapshot=epoch,
                                messages=[self.msg('first', text=original, days=3),
                                          self.msg('second', text='合成末尾', days=2)])
        epoch = self.store.snapshot(**AUTH)
        offset = body_offset = 0
        pieces = []
        while True:
            rows, (offset, body_offset), more = self.store.timeline_page(
                **AUTH, snapshot=epoch, offset=offset, body_offset=body_offset,
                limit=1, char_limit=1000)
            pieces += [r['text'] for r in rows]
            if not more:
                break
        self.assertEqual(''.join(pieces), original + '合成末尾')
        with self.assertRaises(AccessDenied):
            self.store.timeline_page(**dict(AUTH, actor='4444444444'), snapshot=epoch)


class TimelineContextTests(OwnerContextTests):
    async def test_page_advances_only_after_delivery_and_no_web(self):
        await self.configured()
        await self.receive('微信从头读', 'timeline-1')
        self.assertEqual(self.service.wechat_timeline_cursor[1], 2)
        packet = next(m['content'] for m in self.model.complete.await_args.args[0]
                      if str(m['content']).startswith('<wechat_private_data>'))
        self.assertIn('sequential_page', packet)
        self.assertFalse(self.model.complete.await_args.kwargs['allow_tools'])
        self.assertEqual(self.service.store.history(), [])


class RepeatTests(GroupTests):
    async def test_passive_duplicate_suppressed_but_mention_allowed(self):
        self.group.recent.append({'member_qq': self.config['bot_qq'],
                                  'time': time.time(), 'text': '不接这张图'})
        self.group.current_request = {'trigger': 'random'}
        sent = await self.group.reply('r1', '不能发送那张图片', conversational=True)
        self.assertFalse(sent)
        self.assertEqual(self.group_channel.sent, [])
        self.group.current_request = {'trigger': 'mention'}
        self.assertTrue(await self.group.reply('r2', '不能发送那张图片', conversational=True))


class AutonomousStyleTests(AutonomyTests):
    async def test_autonomous_requests_use_normal_style_only(self):
        a = self.prepare()
        self.service.chat_style = ('平常提示词\nNSFW{ x }\nWord_Block { x }\n'
                                   'Core_Truth { x }\n【角色扮演指令】\n角色词')
        self.assertTrue(await a.run(True))
        for call in self.service.model.complete.await_args_list:
            system = next(m['content'] for m in call.args[0] if m['role'] == 'system')
            self.assertIn('平常提示词', system)
            self.assertIn('鲸鱼娘', system)
            self.assertNotIn('NSFW{', system)
            self.assertNotIn('角色词', system)

