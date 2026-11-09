"""Bounded QQ image input, never a model-accessible URL or filesystem tool."""

import asyncio
import base64
import io
import ipaddress
import json
import re
import socket
import warnings
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

MAX_IMAGE_REQUEST_BYTES = 64 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000
IMAGE_TOKEN_RESERVE = 2048  # Official current upper bound is 1024; retain headroom.
QQ_IMAGE_HOSTS = frozenset(
    {"multimedia.nt.qq.com.cn", "gchat.qpic.cn", "c2cpicdw.qpic.cn", "gxh.vip.qq.com"}
)
MAX_ANIMATION_FRAMES = 4
try:
    FACE_LABELS = json.loads(
        (Path(__file__).resolve().parents[1] / "config/qq-face-labels.json").read_text(
            encoding="utf-8"
        )
    )["labels"]
except (OSError, ValueError, KeyError):
    FACE_LABELS = {}


class ImageInputError(ValueError):
    """Safe, fixed error message suitable for owner diagnostics."""


def checked_image_url(value) -> str:
    if not isinstance(value, str) or len(value) > 8192:
        raise ImageInputError("QQ图片地址无效，请重新发送原图。")
    try:
        url = urlsplit(value)
        valid = (
            url.scheme == "https"
            and url.hostname in QQ_IMAGE_HOSTS
            and url.port in (None, 443)
            and url.username is None
            and url.password is None
            and not url.fragment
            and not any(ord(c) < 33 or ord(c) == 127 for c in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ImageInputError("仅支持QQ直接发送的图片，暂不读取外部链接或本地路径。")
    return value


@dataclass(frozen=True)
class IncomingMessage:
    text: str
    images: tuple[str, ...] = ()
    reply_id: str | None = None
    program_event: bool = False
    attachments: tuple[str, ...] = ()
    forwards: tuple[str, ...] = ()

    @property
    def history_text(self):
        if self.program_event:
            return "[QQ系统事件：对方戳了戳大肥鱼]"
        marker = f"[本条包含{len(self.images)}张图片；原图未保存到聊天历史，已收藏表情另存表情库；后续需要细节请重发]"
        return (
            self.text
            + ("\n" + marker if self.images else "")
            + ("\n[本条有引用；原文不作为本人事实保存]" if self.reply_id else "")
            + ("\n[本条包含群视频或文件；未保存原始内容]" if self.attachments else "")
        )


def parse_message(parts, *, allow_group_media=False) -> IncomingMessage:
    if not isinstance(parts, list) or not 1 <= len(parts) <= 100:
        raise ImageInputError("请发送文字、QQ图片或单条引用；直接语音暂未接入。")
    texts, images, reply_id, attachments, forwards = [], [], None, [], []
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get("data"), dict):
            raise ImageInputError("消息格式无效，请重新发送文字或图片。")
        data = part["data"]
        if part.get("type") == "reply":
            value = data.get("id")
            if (
                isinstance(value, bool)
                or not isinstance(value, (str, int))
                or not re.fullmatch(r"-?[0-9]{1,20}", str(value))
                or reply_id is not None
            ):
                raise ImageInputError("引用消息标识无效，请重新引用一条消息。")
            reply_id = str(value)
        elif part.get("type") == "text" and isinstance(data.get("text"), str):
            texts.append(data["text"])
        elif part.get("type") in {"image", "mface"}:
            url = data.get("url")
            if part["type"] == "mface" and not url:
                emoji = data.get("emoji_id")
                if not isinstance(emoji, str) or not re.fullmatch(
                    r"[a-fA-F0-9]{16,64}", emoji
                ):
                    raise ImageInputError("商城表情标识无效，请重新发送。")
                url = f"https://gxh.vip.qq.com/club/item/parcel/item/{emoji[:2]}/{emoji}/raw300.gif"
            images.append(checked_image_url(url))
            summary = data.get("summary")
            if (
                isinstance(summary, str)
                and summary.strip()
                and (
                    part["type"] == "mface"
                    or data.get("sub_type") == 1
                    or data.get("emoji_id")
                )
            ):
                texts.append("[QQ表情包摘要：" + summary[:80] + "]")
        elif part.get("type") == "forward":
            fid = data.get("id")
            if not isinstance(fid, (str, int)) or isinstance(fid, bool) or not 1 <= len(str(fid)) <= 1024 or len(forwards) >= 3:
                raise ImageInputError("合并转发标识无效，请重新发送。")
            forwards.append(str(fid))
        elif part.get("type") in {"video", "file"} and allow_group_media:
            attachments.append(part["type"])
            if len(attachments) > 3:
                raise ImageInputError("一次最多处理三个群视频或文件，请分开发送。")
        elif part.get("type") in {"dice", "rps"}:
            kind = part["type"]
            result = str(data.get("result", ""))
            texts.append(
                "[QQ骰子"
                + ("：" + result if result in {"1", "2", "3", "4", "5", "6"} else "")
                + "]"
                if kind == "dice"
                else "[QQ猜拳"
                + (
                    "：" + {"1": "石头", "2": "剪刀", "3": "布"}.get(result, "")
                    if result in {"1", "2", "3"}
                    else ""
                )
                + "]"
            )
        elif part.get("type") == "face":
            face_id = str(data.get("id", ""))
            if not re.fullmatch(r"[0-9]{1,6}", face_id):
                raise ImageInputError("QQ表情编号无效。")
            name = FACE_LABELS.get(face_id)
            # Catalog labels come from the installed QQ/NapCat mapping; unknown IDs
            # stay unknown instead of inventing visual semantics.
            texts.append(f"[QQ表情：{name or '未知表情'}，ID={face_id}]")
        else:
            raise ImageInputError(
                "支持文字、QQ图片、表情与单条引用；直接语音暂未接入。"
            )
    text = "".join(texts).strip()
    if len(text) > 3000 or (not text and not images and not reply_id and not attachments and not forwards):
        raise ImageInputError("请发送1～3000字的文字或QQ图片。")
    return IncomingMessage(text, tuple(images), reply_id, attachments=tuple(attachments), forwards=tuple(forwards))


def image_data_url(raw: bytes) -> str:
    if len(raw) > MAX_DOWNLOAD_BYTES:
        raise ImageInputError("单张图片超过8MiB，请压缩后重发。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in {"JPEG", "PNG", "GIF", "WEBP"}:
                    raise ImageInputError("仅支持JPEG、PNG、GIF和WebP图片。")
                if image.width * image.height > MAX_PIXELS or max(image.size) > 8192:
                    raise ImageInputError("图片尺寸过大，请缩小后重发。")
                image.load()
                mime = {
                    "JPEG": "image/jpeg",
                    "PNG": "image/png",
                    "WEBP": "image/webp",
                    "GIF": "image/png",
                }[image.format]
                if image.format == "GIF":
                    # Preserve first-frame resolution losslessly instead of reducing
                    # detail to save cost. Static inputs retain their original bytes.
                    image.seek(0)
                    output = io.BytesIO()
                    image.convert("RGBA").save(output, format="PNG")
                    raw = output.getvalue()
                    if len(raw) > MAX_DOWNLOAD_BYTES:
                        raise ImageInputError(
                            "GIF首帧转换后超过8MiB，请直接发送静态图片。"
                        )
        return "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")
    except ImageInputError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as from_error:
        raise ImageInputError("图片损坏或格式不支持，请重新发送。") from from_error


def image_content_parts(raw: bytes) -> list[dict]:
    """Keep static bytes; send up to four full-resolution chronological frames."""
    static = image_data_url(raw)  # Enforces format, size, decode and pixel limits.
    with Image.open(io.BytesIO(raw)) as image:
        count = getattr(image, "n_frames", 1)
        if count <= 1:
            return [{"type": "image_url", "image_url": {"url": static}}]
        if count > 300 or count * image.width * image.height > 100_000_000:
            raise ImageInputError("动图帧数或解码尺寸过大，请分段发送。")
        indices = sorted(
            {
                round(i * (count - 1) / (min(count, MAX_ANIMATION_FRAMES) - 1))
                for i in range(min(count, MAX_ANIMATION_FRAMES))
            }
        )
        parts, total = [], 0
        for index in indices:
            image.seek(index)
            output = io.BytesIO()
            image.convert("RGBA").save(output, format="PNG")
            frame = output.getvalue()
            total += len(frame)
            if total > MAX_DOWNLOAD_BYTES:
                raise ImageInputError("动图关键帧展开后超过8MiB，请分段发送。")
            parts.extend(
                [
                    {
                        "type": "text",
                        "text": f"[动图关键帧 {index + 1}/{count}，按时间顺序理解，未展示所有帧]",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(frame).decode("ascii")
                        },
                    },
                ]
            )
        return parts


class QQImageLoader:
    def __init__(self):
        self.last_diagnostic = None

    async def load(self, value: str) -> str | list[dict]:
        raw = await self.download_raw(value)
        try:
            parts = image_content_parts(raw)
        except ImageInputError:
            self.last_diagnostic["state"] = "failed"
            raise
        self.last_diagnostic["frames"] = sum(p["type"] == "image_url" for p in parts)
        if len(parts) == 1 and parts[0]["type"] == "image_url":
            return parts[0]["image_url"]["url"]
        return parts

    async def download_raw(self, value: str) -> bytes:
        value = checked_image_url(value)
        host = urlsplit(value).hostname
        diagnostic = {"host": host, "state": "resolving", "attempts": 0, "failures": []}
        self.last_diagnostic = diagnostic
        try:
            async with asyncio.timeout(40):
                addresses = await asyncio.get_running_loop().getaddrinfo(
                    host, 443, type=socket.SOCK_STREAM
                )
                ips = list(dict.fromkeys(row[4][0] for row in addresses))
                if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
                    raise ImageInputError("图片下载地址不可用，请重发QQ原图。")
                ips.sort(key=lambda ip: ipaddress.ip_address(ip).version)
                candidates = (ips * 3)[:3]
                last_error = None
                async with httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(15, connect=6),
                ) as client:
                    for attempt, ip in enumerate(candidates):
                        if attempt:
                            await asyncio.sleep(0.2 * attempt)
                        diagnostic["attempts"] += 1
                        diagnostic["state"] = "downloading"
                        target = httpx.URL(value).copy_with(host=ip)
                        try:
                            # Checked public IP stays pinned; Host/TLS SNI remain the
                            # original allowlisted CDN. No redirects or auth forwarding.
                            async with client.stream(
                                "GET",
                                target,
                                headers={"Host": host},
                                extensions={"sni_hostname": host},
                            ) as response:
                                if (
                                    response.status_code >= 500
                                    or response.status_code == 429
                                ):
                                    raise httpx.HTTPStatusError(
                                        "Transient CDN response",
                                        request=response.request,
                                        response=response,
                                    )
                                if response.status_code != 200:
                                    raise ImageInputError(
                                        "QQ图片下载失败或地址过期，请重新发送。"
                                    )
                                length = response.headers.get("content-length", "0")
                                if (
                                    not length.isdecimal()
                                    or int(length) > MAX_DOWNLOAD_BYTES
                                ):
                                    raise ImageInputError(
                                        "图片过大或下载响应无效，请压缩后重发。"
                                    )
                                raw = bytearray()
                                async for chunk in response.aiter_bytes(
                                    chunk_size=65536
                                ):
                                    raw.extend(chunk)
                                    if len(raw) > MAX_DOWNLOAD_BYTES:
                                        raise ImageInputError(
                                            "单张图片超过8MiB，请压缩后重发。"
                                        )
                            diagnostic.update(
                                state="completed",
                                bytes=len(raw),
                            )
                            return bytes(raw)
                        except (httpx.HTTPError, OSError) as error:
                            last_error = error
                            diagnostic["failures"].append(
                                {
                                    "type": type(error).__name__,
                                    "cause_type": type(error.__cause__).__name__
                                    if error.__cause__
                                    else None,
                                    "ip_version": ipaddress.ip_address(ip).version,
                                    "http_status": getattr(
                                        getattr(error, "response", None),
                                        "status_code",
                                        None,
                                    ),
                                }
                            )
                    raise ImageInputError(
                        "QQ图片下载暂时失败（已进行有限下载重试），请稍后重发。"
                    ) from last_error
        except asyncio.CancelledError:
            diagnostic["state"] = "cancelled"
            raise
        except ImageInputError:
            diagnostic["state"] = "failed"
            raise
        except (httpx.HTTPError, OSError, TimeoutError) as error:
            diagnostic.update(state="failed", final_error_type=type(error).__name__)
            raise ImageInputError(
                "QQ图片下载超时或网络异常，请稍后重新发送。"
            ) from error
