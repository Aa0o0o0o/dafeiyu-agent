"""No model JavaScript/shell, private networks, credentials or executable artifacts."""

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit


class BrowserError(ValueError):
    pass


SENSITIVE_ACTION = re.compile(
    r"登录|登入|注册|付款|支付|购买|下单|转账|删除|注销|发帖|发表评论|发送消息|发布|上传|授权|绑定|订阅|取消订阅|确认订单|同意.*条款|接受.*协议|agree.*terms|accept.*terms|sign.?in|log.?in|sign.?up|buy|checkout|purchase|pay(?:ment)?|delete|remove.account|submit.comment|post.comment|upload|authorize|subscribe",
    re.I,
)
SECRET = re.compile(
    r"sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S+|(?:password|密码|token|令牌|api_key)\s*[:=：]\s*\S+",
    re.I,
)
ALLOWED_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".webm",
    ".mp4",
    ".mp3",
    ".wav",
    ".txt",
    ".csv",
    ".json",
    ".md",
    ".zip",
}
ACTIONS = {
    "open",
    "snapshot",
    "read",
    "find",
    "click",
    "type",
    "press",
    "select",
    "check",
    "hover",
    "drag",
    "scroll",
    "back",
    "forward",
    "reload",
    "tabs",
    "switch_tab",
    "close_tab",
    "screenshot",
    "pdf",
    "download",
    "share",
    "bookmark_add",
    "bookmarks",
    "bookmark_remove",
    "video_list",
    "video_play",
    "video_pause",
    "video_seek",
    "video_rate",
    "video_mute",
    "video_observe",
    "video_transcript",
    "wait",
    "close_session",
    "status",
}
KEYS = {
    "Enter",
    "Tab",
    "Escape",
    "ArrowDown",
    "ArrowUp",
    "ArrowLeft",
    "ArrowRight",
    "PageDown",
    "PageUp",
    "Home",
    "End",
    "Space",
}


class PublicPolicy:
    def __init__(self, test_ports=()):
        self.test_ports = set(test_ports)

    def url(self, value):
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 8192
            or any(ord(c) < 33 for c in value)
        ):
            raise BrowserError("网页地址无效")
        try:
            u = urlsplit(value)
            port = u.port or (443 if u.scheme == "https" else 80)
            if (
                u.scheme not in {"http", "https"}
                or not u.hostname
                or u.username is not None
                or u.password is not None
            ):
                raise ValueError()
            host = u.hostname.rstrip(".").lower()
            if host == "127.0.0.1" and port in self.test_ports:
                return value
            if (
                port not in {80, 443}
                or host.endswith((".local", ".localhost", ".internal", ".lan", ".test"))
                or "." not in host
            ):
                raise ValueError()
            try:
                address = ipaddress.ip_address(host)
                if (
                    not address.is_global
                    or address.is_multicast
                    or address.is_reserved
                    or getattr(address, "ipv4_mapped", None)
                    and not address.ipv4_mapped.is_global
                ):
                    raise ValueError()
            except ValueError:
                if re.fullmatch(r"[0-9.]+", host) or ":" in host:
                    raise
            if re.search(
                r"(?:^|[?&])(?:password|access_token|api_key|session_key|auth_token)=",
                u.query,
                re.I,
            ):
                raise ValueError()
        except (TypeError, ValueError):
            raise BrowserError(
                "仅可打开无账号凭据的公开HTTP/HTTPS网页；不访问本机、内网或文件"
            ) from None
        return value

    async def resolve(self, host, port):
        if host == "127.0.0.1" and port in self.test_ports:
            return host
        self.url(
            ("https" if port == 443 else "http")
            + "://"
            + host
            + (":" + str(port) if port not in {80, 443} else "")
        )
        answers = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
        ips = []
        for answer in answers:
            address = ipaddress.ip_address(answer[4][0])
            if (
                not address.is_global
                or address.is_multicast
                or address.is_reserved
                or getattr(address, "ipv4_mapped", None)
                and not address.ipv4_mapped.is_global
            ):
                raise BrowserError("域名解析到非公网地址，已阻止")
            ips.append(str(address))
        if not ips:
            raise BrowserError("网页域名解析失败")
        return sorted(ips, key=lambda ip: ":" in ip)[0]


def validate_command(value):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("action"), str)
        or value.get("action") not in ACTIONS
    ):
        raise BrowserError("未知浏览器操作")
    allowed = {
        "action",
        "url",
        "new_tab",
        "tab_id",
        "ref",
        "target_ref",
        "text",
        "key",
        "values",
        "enabled",
        "direction",
        "amount",
        "seconds",
        "video",
        "time",
        "rate",
        "share",
        "artifact_id",
        "offset",
        "limit",
        "frame",
    }
    if set(value) - allowed:
        raise BrowserError("浏览器参数含未授权字段")
    if any(
        not isinstance(value.get(k), bool)
        for k in ["new_tab", "enabled", "share"]
        if k in value
    ):
        raise BrowserError("浏览器开关参数无效")
    for k, limit in [
        ("text", 3000),
        ("ref", 60),
        ("target_ref", 60),
        ("tab_id", 60),
        ("artifact_id", 60),
        ("key", 30),
        ("direction", 12),
        ("url", 8192),
    ]:
        if k in value and (not isinstance(value[k], str) or len(value[k]) > limit):
            raise BrowserError("浏览器字符串参数无效")
    if SECRET.search(value.get("text", "")):
        raise BrowserError("不要在QQ浏览器工具中输入密码或令牌")
    for k, low, high in [
        ("amount", 1, 3000),
        ("seconds", 0, 15),
        ("video", 0, 30),
        ("time", 0, 86400),
        ("rate", 0.25, 3),
        ("offset", 0, 500000),
        ("limit", 100, 20000),
        ("frame", 0, 9),
    ]:
        if k in value:
            import math

            x = value[k]
            if (
                isinstance(x, bool)
                or not isinstance(x, (float, int))
                or not math.isfinite(x)
                or not low <= x <= high
            ):
                raise BrowserError("浏览器数值参数无效")
    if "values" in value and (
        not isinstance(value["values"], list)
        or len(value["values"]) > 10
        or any(not isinstance(x, str) or len(x) > 200 for x in value["values"])
    ):
        raise BrowserError("下拉选项无效")
    if "key" in value and value["key"] not in KEYS:
        raise BrowserError("仅允许页面导航/输入按键")
    return dict(value)
