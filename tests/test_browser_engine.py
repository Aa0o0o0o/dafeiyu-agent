"""Real Chromium + real proxy + synthetic pages, never personal sessions or paid APIs."""

import asyncio
import base64
import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.test_conversation import ROOT
from src.browser.broker import BrowserBroker
from src.browser.policy import BrowserError

HTML = r"""<!doctype html><meta charset=utf-8><title>大肥鱼浏览器合成验收</title>
<style>body{font:20px sans-serif;margin:35px;background:#edf5ff;color:#173554}button,input,select{font:inherit;padding:8px;margin:8px}video{display:block;width:480px;height:270px;background:#102031}a{margin:8px}</style>
<h1>大肥鱼 · 浏览器合成验收</h1><p>只包含测试内容，不是用户真实聊天。</p>
<form action=/search role=search><input type=search name=q aria-label=站内搜索><button>搜索</button></form>
<button onclick="document.getElementById('result').textContent='按钮已响应'">更换文字</button>
<button onclick="document.cookie='test_marker=A';localStorage.setItem('test_marker','A')">设置测试标记</button>
<button>确认支付</button><input type=password aria-label=测试密码>
<select aria-label=颜色><option value=blue>蓝色</option><option value=green>绿色</option></select>
<p id=result>还未点击</p><p id=local>empty</p>
<iframe srcdoc="&lt;p&gt;内嵌合成内容&lt;/p&gt;"></iframe><a href="mailto:synthetic@example.com">测试邮件链接</a><a href=/second>下一页</a><a href=/sample.txt download>下载测试文本</a><a href=/danger.exe download>下载测试程序</a>
<video id=v muted controls></video><script>
document.getElementById('local').textContent=localStorage.getItem('test_marker')||'empty';
(async()=>{let canvas=document.createElement('canvas');canvas.width=480;canvas.height=270;let ctx=canvas.getContext('2d'),n=0;let timer=setInterval(()=>{ctx.fillStyle=n++%2?'#267cb8':'#25a69a';ctx.fillRect(0,0,480,270);ctx.fillStyle='white';ctx.font='32px sans-serif';ctx.fillText('Synthetic fish video '+n,30,140)},100);let stream=canvas.captureStream(10),rec=new MediaRecorder(stream,{mimeType:'video/webm;codecs=vp8'}),parts=[];rec.ondataavailable=e=>parts.push(e.data);rec.onstop=()=>{clearInterval(timer);stream.getTracks().forEach(t=>t.stop());v.src=URL.createObjectURL(new Blob(parts,{type:'video/webm'}));v.loop=true;let track=v.addTextTrack('subtitles','合成字幕','zh');track.addCue(new VTTCue(0,2,'这是合成测试字幕'));track.mode='showing'};rec.start();setTimeout(()=>rec.stop(),1800)})();
</script><div style="height:1200px">向下滚动测试</div>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        status = 200
        kind = "text/html; charset=utf-8"
        extra = {}
        if self.path.startswith("/sample.txt"):
            body = "synthetic download only".encode()
            kind = "text/plain"
            extra["Content-Disposition"] = "attachment; filename=sample.txt"
        elif self.path.startswith("/danger.exe"):
            body = b"synthetic forbidden type"
            kind = "application/octet-stream"
            extra["Content-Disposition"] = "attachment; filename=danger.exe"
        elif self.path.startswith("/second"):
            body = (
                b"<title>Second</title><h1>Second synthetic page</h1><a href=/>Back</a>"
            )
        elif self.path.startswith("/cookie"):
            body = (
                "<title>Cookie</title><p>"
                + self.headers.get("Cookie", "empty")
                + "</p>"
            ).encode()
        elif self.path.startswith("/search"):
            body = ("<title>Search</title><p>" + self.path + "</p>").encode()
        else:
            body = HTML.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(
    (ROOT / "config/browser-runtime-manifest.json").is_file(),
    "Optional project browser runtime not installed",
)
class BrowserEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.broker = BrowserBroker(
            ROOT,
            test_ports=[self.server.server_port],
            worker_root=Path(self.tmp.name) / "browser",
        )
        self.scope = "synthetic:group:111111"
        self.actor = "12345001"

    async def asyncTearDown(self):
        await self.broker.close()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    async def call(self, action, **kwargs):
        return await self.broker.execute(
            self.scope, self.actor, dict(action=action, **kwargs)
        )

    def ref(self, data, label):
        return next(
            e["ref"]
            for frame in data["frames"]
            for e in frame["elements"]
            if e["label"] == label
        )

    async def test_real_navigation_refs_inputs_tabs_and_stale_ref_rejection(self):
        data = await self.call("open", url=self.url)
        self.assertIn("内嵌合成内容", str(data))
        longread = await self.call("read", offset=0, limit=100)
        self.assertLessEqual(len(longread["text"]), 100)
        stale = self.ref(data, "更换文字")
        data = await self.call("click", ref=stale)
        self.assertIn("按钮已响应", str(data))
        with self.assertRaises(BrowserError):
            await self.call("click", ref=stale)
        data = await self.call(
            "type", ref=self.ref(data, "站内搜索"), text="synthetic query"
        )
        data = await self.call("press", ref=self.ref(data, "站内搜索"), key="Enter")
        self.assertIn("/search?q=synthetic", data["url"])
        await self.call("open", url=self.url + "/second", new_tab=True)
        tabs = await self.call("tabs")
        self.assertEqual(len(tabs["tabs"]), 2)
        await self.call("close_tab")
        tabs = await self.call("tabs")
        self.assertEqual(len(tabs["tabs"]), 1)

    async def test_cookies_and_artifacts_do_not_cross_actors_or_groups(self):
        data = await self.call("open", url=self.url)
        await self.call("click", ref=self.ref(data, "设置测试标记"))
        data = await self.call("open", url=self.url + "/cookie")
        self.assertIn("test_marker=A", str(data))
        other = await self.broker.execute(
            self.scope, "12345002", {"action": "open", "url": self.url + "/cookie"}
        )
        self.assertNotIn("test_marker=A", str(other))
        other = await self.broker.execute(
            "synthetic:group:222222",
            self.actor,
            {"action": "open", "url": self.url + "/cookie"},
        )
        self.assertNotIn("test_marker=A", str(other))
        shot = await self.call("screenshot")
        token = shot["artifact"]["id"]
        with self.assertRaises(BrowserError):
            self.broker.artifact(self.scope, "12345002", token)
        with self.assertRaises(BrowserError):
            await self.broker.execute(
                self.scope, "12345002", {"action": "share", "artifact_id": token}
            )

    async def test_actual_video_play_pause_frames_and_caption(self):
        await self.call("open", url=self.url)
        await self.call("wait", seconds=3)
        videos = await self.call("video_list")
        self.assertEqual(len(videos["videos"]), 1)
        observed = await self.call("video_observe", seconds=3)
        self.assertEqual(len(observed["images"]), 3)
        self.assertGreater(observed["observations"][0]["width"], 0)
        self.assertIn("合成测试字幕", str(observed["transcript"]))
        paused = await self.call("video_pause")
        self.assertTrue(paused["video"]["paused"])
        await self.call("video_seek", time=0.5)
        encoded = observed["images"][0]["image_url"]["url"].partition(",")[2]
        (ROOT / "evidence/browser-video-sample.jpg").write_bytes(
            base64.b64decode(encoded)
        )
        evidence = {k: v for k, v in observed.items() if k != "images"}
        (ROOT / "evidence/browser-video-fixture.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def test_screenshot_pdf_download_and_sensitive_actions(self):
        data = await self.call("open", url=self.url)
        with self.assertRaises(BrowserError):
            await self.call("click", ref=self.ref(data, "确认支付"))
        with self.assertRaises(BrowserError):
            await self.call("click", ref=self.ref(data, "测试邮件链接"))
        with self.assertRaises(BrowserError):
            await self.call(
                "type", ref=self.ref(data, "测试密码"), text="not-a-real-secret"
            )
        await self.call("scroll", direction="down", amount=550)
        screenshot = await self.call("screenshot")
        image = self.broker.artifact(
            self.scope, self.actor, screenshot["artifact"]["id"]
        )
        shutil.copy2(image["path"], ROOT / "evidence/browser-synthetic.jpg")
        pdf = await self.call("pdf")
        artifact = self.broker.artifact(self.scope, self.actor, pdf["artifact"]["id"])
        self.assertTrue(Path(artifact["path"]).read_bytes().startswith(b"%PDF"))
        await self.call("scroll", direction="top")
        data = await self.call("snapshot")
        download = await self.call("download", ref=self.ref(data, "下载测试文本"))
        artifact = self.broker.artifact(
            self.scope, self.actor, download["artifact"]["id"]
        )
        self.assertEqual(Path(artifact["path"]).read_text(), "synthetic download only")
        data = await self.call("snapshot")
        with self.assertRaises(BrowserError):
            await self.call("download", ref=self.ref(data, "下载测试程序"))

    async def test_cancel_closes_only_callers_context(self):
        await self.call("open", url=self.url)
        await self.broker.execute(
            self.scope, "12345002", {"action": "open", "url": self.url + "/second"}
        )
        task = asyncio.create_task(self.call("wait", seconds=3))
        await asyncio.sleep(0.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.3)
        other = await self.broker.execute(self.scope, "12345002", {"action": "tabs"})
        self.assertEqual(len(other["tabs"]), 1)
        own = await self.call("status")
        self.assertEqual(own["open_tabs"], 0)
