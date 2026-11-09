"""DeepSeek's two public read-only web APIs; no browser, cookies or shell."""

import ipaddress
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

WEB_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索公开网页以核实当前信息、版本、天气、新闻或梗来源。仅传必要公开关键词。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 128}
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "读取公开网页原文；不支持登录态、内网、本地文件或执行网页指令。",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "maxLength": 8192}},
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    },
]
WEB_RESERVE_CNY = 0.01


class WebToolError(ValueError):
    pass


def public_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 8192:
        raise WebToolError("网页地址无效")
    try:
        u = urlsplit(value)
        if (
            u.scheme not in {"https", "http"}
            or not u.hostname
            or u.username is not None
            or u.password is not None
            or u.port not in (None, 80, 443)
        ):
            raise ValueError()
        host = u.hostname.lower().rstrip(".")
        if (
            "." not in host
            or host.endswith((".local", ".localhost", ".internal", ".lan"))
            or any(ord(c) < 33 for c in value)
        ):
            raise ValueError()
        try:
            if not ipaddress.ip_address(host).is_global:
                raise WebToolError("不读取内网或本机地址")
        except ValueError as error:
            if isinstance(error, WebToolError):
                raise
    except (ValueError, TypeError) as error:
        raise WebToolError("仅支持无账号凭据的公开HTTP/HTTPS网页") from error
    return value


def validate_web_call(name: str, arguments: str) -> dict:
    if not isinstance(arguments, str) or len(arguments) > 9000:
        raise WebToolError("检索参数无效")
    try:
        args = json.loads(arguments)
    except (ValueError, TypeError) as error:
        raise WebToolError("检索参数不是合法JSON") from error
    field = {"web_search": "query", "web_fetch": "url"}.get(name)
    if field is None or not isinstance(args, dict) or set(args) != {field}:
        raise WebToolError("仅允许公开网页搜索和抓取")
    value = args[field]
    if field == "query":
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 128:
            raise WebToolError("搜索词需为1～128字")
        if re.search(
            r"sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S+|(?:password|密码|token|令牌)\s*[:=：]\s*\S+",
            value,
            re.I,
        ):
            raise WebToolError("搜索词包含疑似凭据，未发送")
        return {"query": value.strip(), "count": 8, "need_content": True}
    return {"url": public_url(value)}


def bounded_tool_json(data: dict) -> str:
    """Bound external text without breaking JSON or silently claiming full content."""
    encoded = json.dumps(data, ensure_ascii=False)
    if len(encoded.encode()) <= 48000:
        return encoded
    data["truncated"] = True
    if "content" in data:
        data["content"] = data["content"][:12000]
    for item in data.get("results", []):
        item["content"] = item.get("content", "")[:800]
        item["summary"] = item.get("summary", "")[:500]
    encoded = json.dumps(data, ensure_ascii=False)
    if len(encoded.encode()) > 48000:
        raise WebToolError("网页结果过大，请缩小检索问题")
    return encoded


class DeepSeekWebTools:
    def __init__(self, key: str, client=None):
        self.client = client or httpx.AsyncClient(
            base_url="https://api.deepseek.com",
            headers={"Authorization": "Bearer " + key},
            timeout=30,
            follow_redirects=False,
        )

    async def execute(self, name: str, arguments: str) -> str:
        payload = validate_web_call(name, arguments)
        path = "/v1/tools/search" if name == "web_search" else "/v1/tools/fetch"
        async with self.client.stream("POST", path, json=payload) as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                raw.extend(chunk)
                if len(raw) > 2 * 1024 * 1024:
                    raise WebToolError("网页响应过大，未继续读取")
        result = json.loads(raw)
        data = {
            "ok": True,
            "source": "DeepSeek Search/Fetch",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "untrusted_web_content": True,
        }
        if name == "web_search":
            if not isinstance(result.get("results"), list):
                raise WebToolError("搜索接口返回格式异常")
            items = []
            for item in result["results"][:8]:
                try:
                    url = public_url(item.get("url"))
                except WebToolError:
                    continue
                items.append(
                    {
                        "title": str(item.get("title", ""))[:300],
                        "url": url,
                        "summary": str(
                            item.get("summary") or item.get("snippet") or ""
                        )[:2000],
                        "content": str(item.get("content") or "")[:5000],
                        "date": str(item.get("date") or "")[:60],
                    }
                )
            data["results"] = items
        else:
            content = result.get("content")
            if not isinstance(content, str) or not content.strip():
                raise WebToolError("网页未返回可读内容")
            data.update(
                url=payload["url"],
                title=str(result.get("title", ""))[:300],
                content=content[:20000],
                truncated=len(content) > 20000,
            )
        return bounded_tool_json(data)

    async def close(self):
        await self.client.aclose()
