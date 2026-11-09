"""Current-account contact verification and permanently bound private routes."""

import asyncio
import time
from .conversation import qq_id


class ContactDirectory:
    def __init__(self, config, read_action):
        self.config, self.read_action = config, read_action
        self.friends, self.groups = set(), set()
        self.valid_until = 0
        self.lock = asyncio.Lock()
        self.last_error = None

    async def refresh(self):
        async with self.lock:
            if time.monotonic() < self.valid_until:
                return
            try:
                friends = await self.read_action("get_friend_list")
                groups = await self.read_action("get_group_list")
                if not isinstance(friends, list) or not isinstance(groups, list):
                    raise ValueError("Invalid contact response")
                self.friends = {
                    qq_id(row.get("user_id"))
                    for row in friends
                    if isinstance(row, dict)
                } - {None, self.config["bot_qq"]}
                self.groups = {
                    qq_id(row.get("group_id"))
                    for row in groups
                    if isinstance(row, dict)
                } - {None}
                self.valid_until = time.monotonic() + 30
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception:
                self.valid_until = 0
                self.last_error = "好友/群名单暂时无法核验"
                raise ConnectionError(self.last_error) from None

    async def allowed(self, kind, target):
        target = qq_id(target)
        if not target or target == self.config["bot_qq"]:
            return False
        expanded = (
            self.config.get("private_all_friends", False)
            if kind == "private"
            else self.config.get("group_all_joined", False)
        )
        if not expanded:
            return (
                target == self.config["owner_qq"]
                if kind == "private"
                else target in self.config.get("group_ids", [])
            )
        try:
            await self.refresh()
        except ConnectionError:
            return False
        return target in (self.friends if kind == "private" else self.groups)


class PrivateChannel:
    def __init__(self, adapter, peer):
        self.adapter, self.peer = adapter, peer

    async def send_text(self, text):
        return await self.adapter.send_private_text(self.peer, text)

    async def read_forward(self, forward_id):
        return await self.adapter.read_forward(forward_id, peer_qq=self.peer)

    async def fetch_quoted(self, message_id):
        return await self.adapter.fetch_quoted(message_id, peer_qq=self.peer)

    async def send_browser_artifact(self, artifact):
        return await self.adapter.send_browser_artifact(artifact, peer_qq=self.peer)

    async def send_sticker(self, token):
        return await self.adapter.send_sticker(token, peer_qq=self.peer)

    async def send_expression(self, kind, value=""):
        return await self.adapter.send_expression(kind, value, peer_qq=self.peer)

    async def send_record(self, asset):
        return await self.adapter.send_record(asset, peer_qq=self.peer)
