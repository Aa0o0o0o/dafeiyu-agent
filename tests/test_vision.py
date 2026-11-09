"""Synthetic images and mocked network only; no QQ, real URLs or API keys."""

import asyncio
import base64
import io
import socket
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from src.vision import (
    ImageInputError,
    QQImageLoader,
    checked_image_url,
    image_data_url,
    parse_message,
    MAX_DOWNLOAD_BYTES,
)
from tests import test_conversation as fixtures

event = fixtures.event

URL = "https://gchat.qpic.cn/synthetic?test=1"


def png():
    output = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(output, format="PNG")
    return output.getvalue()


def image_event(mid="image", text="这是什么颜色？"):
    return event(
        mid=mid,
        message=[
            {"type": "text", "data": {"text": text}},
            {"type": "image", "data": {"url": URL, "file": "ignored-local-path"}},
        ],
    )


class ImageParsingTests(unittest.TestCase):
    def test_unknown_face_stays_unknown_and_bad_market_id_rejected(self):
        parsed = parse_message([{"type": "face", "data": {"id": "999999"}}])
        self.assertIn("未知表情", parsed.text)
        for value in ["../../private", "https://evil.invalid", "ab/not-hex"]:
            with self.assertRaises(ImageInputError):
                parse_message([{"type": "mface", "data": {"emoji_id": value}}])

    def test_market_sticker_domain_and_native_face(self):
        url = "https://gxh.vip.qq.com/club/item/parcel/item/ab/abcdef/raw300.gif"
        self.assertEqual(checked_image_url(url), url)
        face = parse_message(
            [{"type": "face", "data": {"id": "14", "raw": {"faceText": "微笑"}}}]
        )
        self.assertIn("QQ表情", face.text)
        self.assertIn("14", face.text)
        market = parse_message(
            [
                {
                    "type": "mface",
                    "data": {"emoji_id": "ab0123456789abcdef", "summary": "开心"},
                }
            ]
        )
        self.assertTrue(market.images[0].startswith("https://gxh.vip.qq.com/"))

    def test_animated_sticker_preserves_several_full_resolution_frames(self):
        from src.vision import image_content_parts

        frames = [
            Image.new("RGB", (100, 80), color)
            for color in ["red", "green", "blue", "yellow"]
        ]
        output = io.BytesIO()
        frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=100,
            loop=0,
        )
        parts = image_content_parts(output.getvalue())
        images = [p for p in parts if p["type"] == "image_url"]
        self.assertEqual(len(images), 4)
        for part in images:
            raw = base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
            with Image.open(io.BytesIO(raw)) as image:
                self.assertEqual(image.size, (100, 80))

    def test_url_boundary(self):
        self.assertEqual(checked_image_url(URL), URL)
        for value in [
            "http://gchat.qpic.cn/image",
            "https://127.0.0.1/private",
            "file:///C:/secret",
            "https://gchat.qpic.cn.attacker.invalid/img",
            "https://user:pass@gchat.qpic.cn/img",
            "https://gchat.qpic.cn:8080/img",
            "https://gchat.qpic.cn/img#fragment",
            "https://example.com/img",
            "https://gchat.qpic.cn/img\n",
            None,
        ]:
            with self.subTest(value=value), self.assertRaises(ImageInputError):
                checked_image_url(value)

    def test_mixed_message_and_limits(self):
        parsed = parse_message(image_event()["message"])
        self.assertEqual(parsed.images, (URL,))
        for parts in [
            [],
            "[CQ:image]",
            [None],
            [{"type": "image", "data": {"file": "C:/x"}}],
            [{"type": "text", "data": {"text": "a" * 3001}}],
            [{"type": "reply", "data": {"id": "x"}}],
        ]:
            with (
                self.subTest(parts=str(parts)[:80]),
                self.assertRaises(ImageInputError),
            ):
                parse_message(parts)

    def test_decode_by_content_without_lossy_compression(self):
        self.assertTrue(image_data_url(png()).startswith("data:image/png;base64,"))
        with self.assertRaises(ImageInputError):
            image_data_url(b"<svg>not an image</svg>")
        with self.assertRaises(ImageInputError):
            image_data_url(b"0" * (MAX_DOWNLOAD_BYTES + 1))

    def test_oversize_dimensions(self):
        output = io.BytesIO()
        Image.new("RGB", (8193, 1)).save(output, format="PNG")
        with self.assertRaises(ImageInputError):
            image_data_url(output.getvalue())

    def test_original_resolution_and_bytes_are_preserved(self):
        output = io.BytesIO()
        Image.new("RGB", (2400, 1800), "green").save(output, format="JPEG", quality=95)
        raw = output.getvalue()
        encoded = image_data_url(raw)
        restored = base64.b64decode(encoded.split(",", 1)[1])
        self.assertEqual(restored, raw)
        with Image.open(io.BytesIO(restored)) as image:
            self.assertEqual(image.size, (2400, 1800))


class ImageNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_public_ip_transient_error_is_retried(self):
        seen = []

        def handler(request):
            seen.append(request.url.host)
            if len(seen) == 1:
                raise httpx.ReadError("synthetic interrupted transfer", request=request)
            return httpx.Response(200, content=png())

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with (
            patch.object(
                asyncio.get_running_loop(),
                "getaddrinfo",
                AsyncMock(return_value=addresses),
            ),
            patch("src.vision.httpx.AsyncClient", return_value=client),
        ):
            self.assertTrue(await QQImageLoader().load(URL))
        self.assertEqual(len(seen), 2)

    async def test_download_attempts_bounded_and_diagnostic_has_no_url_or_token(self):
        seen = []

        def handler(request):
            seen.append(request.url.host)
            raise httpx.ConnectTimeout("synthetic", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
            for ip in ["8.8.8.8", "8.8.8.8", "1.1.1.1", "9.9.9.9", "208.67.222.222"]
        ]
        loader = QQImageLoader()
        with (
            patch.object(
                asyncio.get_running_loop(),
                "getaddrinfo",
                AsyncMock(return_value=addresses),
            ),
            patch("src.vision.httpx.AsyncClient", return_value=client),
        ):
            with self.assertRaises(ImageInputError):
                await loader.load(URL + "&rkey=synthetic-private-key")
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen)), 3)
        self.assertEqual(loader.last_diagnostic["state"], "failed")
        self.assertNotIn("synthetic-private-key", str(loader.last_diagnostic))
        self.assertNotIn("?", str(loader.last_diagnostic))

    async def test_first_cdn_ip_timeout_tries_next_public_ip(self):
        seen = []

        def handler(request):
            seen.append(request.url.host)
            if request.url.host == "8.8.8.8":
                raise httpx.ConnectTimeout(
                    "synthetic first address failure", request=request
                )
            return httpx.Response(200, content=png())

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
            for ip in ["8.8.8.8", "1.1.1.1"]
        ]
        with (
            patch.object(
                asyncio.get_running_loop(),
                "getaddrinfo",
                AsyncMock(return_value=addresses),
            ),
            patch("src.vision.httpx.AsyncClient", return_value=client),
        ):
            result = await QQImageLoader().load(URL)
        self.assertTrue(result)
        self.assertEqual(seen, ["8.8.8.8", "1.1.1.1"])

    async def test_dns_pinned_download_without_proxy_or_redirect(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, content=png())

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with (
            patch.object(
                asyncio.get_running_loop(),
                "getaddrinfo",
                AsyncMock(return_value=addresses),
            ),
            patch("src.vision.httpx.AsyncClient", return_value=client) as factory,
        ):
            value = await QQImageLoader().load(URL)
        self.assertTrue(value.startswith("data:image/png;base64,"))
        self.assertEqual(seen[0].url.host, "8.8.8.8")
        self.assertEqual(seen[0].headers["host"], "gchat.qpic.cn")
        self.assertEqual(seen[0].extensions["sni_hostname"], "gchat.qpic.cn")
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertFalse(factory.call_args.kwargs["follow_redirects"])

    async def test_private_dns_never_connects(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with (
            patch.object(
                asyncio.get_running_loop(),
                "getaddrinfo",
                AsyncMock(return_value=addresses),
            ),
            patch("src.vision.httpx.AsyncClient") as client,
        ):
            with self.assertRaises(ImageInputError):
                await QQImageLoader().load(URL)
            client.assert_not_called()

    async def test_redirect_and_oversize_do_not_download_target(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        for response in [
            httpx.Response(302, headers={"location": "http://127.0.0.1"}),
            httpx.Response(
                200, headers={"content-length": str(MAX_DOWNLOAD_BYTES + 1)}
            ),
        ]:
            seen = []

            def handler(request):
                seen.append(request)
                return response

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with (
                patch.object(
                    asyncio.get_running_loop(),
                    "getaddrinfo",
                    AsyncMock(return_value=addresses),
                ),
                patch("src.vision.httpx.AsyncClient", return_value=client),
            ):
                with self.assertRaises(ImageInputError):
                    await QQImageLoader().load(URL)
            self.assertEqual(len(seen), 1)


class VisionConversationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ConversationTests.asyncSetUp
    asyncTearDown = fixtures.ConversationTests.asyncTearDown
    receive = fixtures.ConversationTests.receive
    idle = fixtures.ConversationTests.idle

    async def test_native_face_is_understood_as_labeled_text(self):
        self.service.image_loader = AsyncMock()
        await self.receive(event(message=[{"type": "face", "data": {"id": "14"}}]))
        await self.idle()
        self.assertIn("微笑", self.model.calls[0][-1]["content"])
        self.service.image_loader.load.assert_not_called()

    async def test_animation_frames_reach_model_and_are_reserved(self):
        from src.vision import image_content_parts

        frames = [
            Image.new("RGB", (20, 20), c) for c in ["red", "green", "blue", "yellow"]
        ]
        out = io.BytesIO()
        frames[0].save(
            out, format="GIF", save_all=True, append_images=frames[1:], duration=100
        )
        self.service.image_loader = AsyncMock()
        self.service.image_loader.load.return_value = image_content_parts(
            out.getvalue()
        )
        await self.receive(image_event())
        await self.idle()
        parts = self.model.calls[0][-1]["content"]
        self.assertEqual(sum(p["type"] == "image_url" for p in parts), 4)
        self.assertGreater(self.service.store.usage()[1], 0.75)
        self.assertNotIn("base64", str(self.service.store.history()))

    async def test_animation_budget_does_not_silently_drop_frames(self):
        self.service.config["budget_cny"] = 0.72
        self.service.image_loader = AsyncMock()
        self.service.image_loader.load.return_value = [
            {"type": "image_url", "image_url": {"url": image_data_url(png())}}
        ] * 4
        await self.receive(image_event())
        await self.idle()
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.service.store.usage()[0], 0)
        self.assertIn("上限", self.channel.sent[-1])

    async def test_images_reach_model_and_are_not_persisted(self):
        loader = AsyncMock()
        loader.load.return_value = image_data_url(png())
        self.service.image_loader = loader
        await self.receive(image_event())
        await self.idle()
        sent = self.model.calls[0]
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[-1]["role"], "user")
        self.assertEqual(sent[-1]["content"][1]["type"], "image_url")
        history = str(self.service.store.history())
        self.assertNotIn("base64", history)
        self.assertNotIn(URL, history)
        self.assertIn("原图未保存", history)
        count, reserved = self.service.store.usage()
        self.assertEqual(count, 1)
        self.assertGreater(reserved, 0.65)
        await self.receive(image_event())
        await self.idle()
        self.assertEqual(loader.load.await_count, 1)

    async def test_image_only(self):
        self.service.image_loader = AsyncMock()
        self.service.image_loader.load.return_value = image_data_url(png())
        await self.receive(image_event(text=""))
        await self.idle()
        self.assertEqual(self.model.calls[0][-1]["content"][0]["type"], "text")

    async def test_wrong_owner_image_not_fetched(self):
        self.service.image_loader = AsyncMock()
        value = image_event()
        value["user_id"] = 12345678
        await self.receive(value)
        self.service.image_loader.load.assert_not_called()
        self.assertEqual(self.model.calls, [])

    async def test_download_failure_no_paid_call(self):
        self.service.image_loader = AsyncMock()
        self.service.image_loader.load.side_effect = ImageInputError(
            "QQ图片下载失败，请重发。"
        )
        await self.receive(image_event())
        await self.idle()
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.service.store.usage()[0], 0)
        self.assertIn("图片下载失败", self.channel.sent[0])
        self.assertNotIn("。。", self.channel.sent[0])
        self.assertIn("本次没有产生模型请求", self.channel.sent[0])

    async def test_cancel_during_download(self):
        started = asyncio.Event()

        async def load(_):
            started.set()
            await asyncio.sleep(10)

        self.service.image_loader = AsyncMock()
        self.service.image_loader.load.side_effect = load
        await self.receive(image_event())
        await asyncio.wait_for(started.wait(), 1)
        await self.receive(event("大肥鱼 停止", "stop-image"))
        await self.idle()
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.service.store.usage()[0], 0)

    async def test_image_budget_before_download(self):
        self.service.config["budget_cny"] = 0.01
        self.service.image_loader = AsyncMock()
        await self.receive(image_event())
        await self.idle()
        self.service.image_loader.load.assert_not_called()
        self.assertEqual(self.model.calls, [])
        self.assertIn("上限", self.channel.sent[0])

    async def test_image_with_command_text_does_not_run_command(self):
        self.service.image_loader = AsyncMock()
        self.service.image_loader.load.return_value = image_data_url(png())
        self.service.store.save_history([{"role": "user", "content": "previous"}])
        await self.receive(image_event(text="大肥鱼 新对话"))
        await self.idle()
        self.assertEqual(len(self.model.calls), 1)
        self.assertTrue(any(m["content"] == "previous" for m in self.model.calls[0]))


if __name__ == "__main__":
    unittest.main()
