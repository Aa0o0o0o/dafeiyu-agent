"""Bounded read-only QQ profiles, avatars and visible QZone posts. No writes."""

import asyncio
import json
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit, urljoin

import httpx

from .vision import image_content_parts


PROFILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_member_profile",
        "description": "必要时只读当前会话相关成员的QQ昵称、群名片、签名，可选择头像和最多3条可见QQ空间动态。不是每轮调用；不扫描群成员，不推断敏感属性。",
        "parameters": {
            "type": "object",
            "properties": {
                "qq": {
                    "type": "string",
                    "description": "当前说话者、近期成员或被明确@的目标QQ",
                },
                "include_avatar": {"type": "boolean"},
                "include_qzone": {"type": "boolean"},
            },
            "required": ["qq"],
            "additionalProperties": False,
        },
    },
}


class ProfileError(ValueError):
    pass


async def bounded_get(client, url, **kwargs):
    async with client.stream("GET", url, **kwargs) as response:
        response.raise_for_status()
        raw = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            raw.extend(chunk)
            if len(raw) > 2 * 1024 * 1024:
                raise ProfileError("资料响应过大，未继续读取。")
        return bytes(raw)


async def load_avatar(qq):
    url = f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
    async with httpx.AsyncClient(
        timeout=15, follow_redirects=False, trust_env=False
    ) as client:
        # Only these Tencent hosts; no cookies, arbitrary URL or local files.
        for _ in range(2):
            async with client.stream("GET", url) as response:
                if response.status_code in {301, 302, 307, 308}:
                    url = urljoin(url, response.headers.get("location", ""))
                    u = urlsplit(url)
                    if (
                        u.scheme != "https"
                        or u.hostname not in {"q1.qlogo.cn", "thirdqq.qlogo.cn"}
                        or u.username
                        or u.password
                        or u.port not in (None, 443)
                    ):
                        raise ProfileError("头像跳转不在允许的腾讯域名内。")
                    continue
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise ProfileError("头像过大，未继续读取。")
                return image_content_parts(bytes(raw))
    raise ProfileError("头像地址暂不可用。")


class MemberProfiles:
    def __init__(
        self,
        read_action,
        config,
        group_id=None,
        *,
        qzone_client=None,
        avatar_loader=None,
        peer_qq=None,
    ):
        self.read_action, self.config, self.group_id = read_action, config, group_id
        self.peer_qq = peer_qq or config["owner_qq"]
        self.qzone_client = qzone_client
        self.avatar_loader = avatar_loader or load_avatar
        self.cache, self.verified_members = {}, {}
        self.last_diagnostic = None

    async def read(self, arguments, allowed):
        if not isinstance(arguments, str) or len(arguments) > 1000:
            raise ProfileError("资料查询参数无效。")
        try:
            args = json.loads(arguments)
        except ValueError as error:
            raise ProfileError("资料查询需要合法JSON。") from error
        if (
            not isinstance(args, dict)
            or set(args) - {"qq", "include_avatar", "include_qzone"}
            or not isinstance(args.get("qq"), str)
            or args.get("qq") not in allowed
        ):
            raise ProfileError("只可查看当前会话相关的已出现或明确@成员。")
        if any(
            not isinstance(args.get(k, False), bool)
            for k in ("include_avatar", "include_qzone")
        ):
            raise ProfileError("资料选项无效。")
        qq = args["qq"]
        if not self.group_id and qq not in {
            self.peer_qq,
            self.config["bot_qq"],
        }:
            raise ProfileError("私聊只开放当前对话者与机器人自身资料。")
        key = (qq, args.get("include_avatar", False), args.get("include_qzone", False))
        cached = self.cache.get(key)
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        if self.group_id:
            info = await self.read_action(
                "get_group_member_info",
                group_id=int(self.group_id),
                user_id=int(qq),
                no_cache=False,
            )
            if (
                not isinstance(info, dict)
                or str(info.get("user_id")) != qq
                or str(info.get("group_id")) != self.group_id
            ):
                raise ProfileError("无法核验该用户仍是当前群成员。")
            self.verified_members[qq] = time.time()
            try:
                public = await self.read_action(
                    "get_stranger_info", user_id=int(qq), no_cache=False
                )
                if isinstance(public, dict) and str(public.get("user_id")) == qq:
                    info = {**public, **info}
            except Exception:
                pass
        else:
            info = await self.read_action(
                "get_stranger_info", user_id=int(qq), no_cache=False
            )
            if not isinstance(info, dict) or str(info.get("user_id")) != qq:
                raise ProfileError("资料身份无法核验。")
        data = {
            "ok": True,
            "qq": qq,
            "source": "QQ可见资料卡",
            "retrieved_at": time.time(),
            "untrusted_profile_data": True,
            "profile": {
                k: str(info[k])[:500]
                for k in ("nickname", "card", "title", "long_nick", "signature")
                if info.get(k)
            },
        }
        if self.group_id:
            data["group_role"] = (
                info.get("role", "unknown")
                if info.get("role") in {"owner", "admin", "member"}
                else "unknown"
            )
        images = []
        if args.get("include_avatar"):
            try:
                images = await asyncio.wait_for(self.avatar_loader(qq), 20)
                data["avatar"] = (
                    "已读取腾讯接口提供的头像，附图仅代表头像内容，不推断敏感属性。"
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                data["avatar"] = "头像暂不可读，未看到实际图像。"
        if args.get("include_qzone"):
            try:
                data["qzone"] = await asyncio.wait_for(self.read_qzone(qq), 20)
            except asyncio.CancelledError:
                raise
            except Exception:
                data["qzone"] = {
                    "status": "unavailable",
                    "note": "空间暂不可读取，可能未授权、登录态不足或接口限制；不能判断对方没有动态。",
                }
        result = (data, images)
        self.last_diagnostic = {
            "profile": "ok",
            "avatar_requested": args.get("include_avatar", False),
            "avatar_read": bool(images),
            "qzone_status": data.get("qzone", {}).get("status", "not_requested"),
        }
        self.cache[key] = (time.time(), result)
        # Small per-scope cache, never saved or sent to other chat scopes.
        self.cache = {k: v for k, v in self.cache.items() if time.time() - v[0] < 300}
        self.verified_members = {
            k: v for k, v in self.verified_members.items() if time.time() - v < 300
        }
        while len(self.cache) > 16:
            self.cache.pop(next(iter(self.cache)))
        return result

    async def read_qzone(self, qq):
        auth = await self.read_action("get_cookies", domain="user.qzone.qq.com")
        jar = SimpleCookie()
        jar.load(auth.get("cookies", "") if isinstance(auth, dict) else "")
        key = jar.get("p_skey") or jar.get("skey")
        if key is None or not key.value:
            raise ProfileError("当前QQ未提供可用空间登录态。")
        uin = jar.get("p_uin") or jar.get("uin")
        if uin is None or uin.value.lstrip("o0") != self.config["bot_qq"]:
            raise ProfileError("空间账号身份无法核验。")
        gtk = 5381
        for char in key.value:
            gtk = ((gtk << 5) + gtk + ord(char)) & 0x7FFFFFFF
        cookie = "; ".join(
            f"{name}={jar[name].value}"
            for name in ("uin", "p_uin", "skey", "p_skey")
            if name in jar
        )
        url = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
        params = {
            "uin": qq,
            "pos": 0,
            "num": 3,
            "replynum": 0,
            "need_private_comment": 0,
            "ftype": 0,
            "sort": 0,
            "g_tk": gtk,
            "format": "jsonp",
            "callback": "_preloadCallback",
        }
        headers = {
            "Cookie": cookie,
            "Referer": f"https://user.qzone.qq.com/{qq}",
            "User-Agent": "Mozilla/5.0",
        }
        if self.qzone_client:
            raw = await bounded_get(
                self.qzone_client, url, params=params, headers=headers
            )
        else:
            async with httpx.AsyncClient(
                timeout=15, follow_redirects=False, trust_env=False
            ) as client:
                raw = await bounded_get(client, url, params=params, headers=headers)
        text = raw.decode("utf-8").strip()
        if text.startswith("_preloadCallback(") and text.rstrip(";").endswith(")"):
            text = text.rstrip(";")[len("_preloadCallback(") : -1]
        data = json.loads(text)
        if data.get("code") != 0 or not isinstance(data.get("msglist"), list):
            raise ProfileError("空间权限或接口限制，未得到可读动态。")
        posts = []
        for item in data["msglist"][:3]:
            # Never export comments, likes, friend feeds or unrelated users' profiles.
            if str(item.get("uin", qq)) != qq:
                continue
            posts.append(
                {
                    "text": str(item.get("content", ""))[:1200],
                    "time": str(
                        item.get("createTime") or item.get("created_time") or ""
                    ),
                    "image_count": len(item.get("pic", []))
                    if isinstance(item.get("pic", []), list)
                    else 0,
                }
            )
        return {
            "status": "ok",
            "source": f"https://user.qzone.qq.com/{qq}",
            "posts": posts,
            "note": "只读取当前账号可见的第一页最多3条文字；未翻页，未看动态配图、评论或相册；空列表不代表从未发过动态。",
        }
