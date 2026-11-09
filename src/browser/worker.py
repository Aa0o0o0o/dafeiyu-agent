"""Fixed JSON-stdio Playwright worker. No model code execution or personal profile."""
# ruff: noqa: E402

import argparse
import asyncio
import base64
import json
import re
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.browser.policy import (
    BrowserError,
    PublicPolicy,
    validate_command,
    SENSITIVE_ACTION,
    ALLOWED_EXTENSIONS,
)  # noqa: E402
from src.browser.proxy import PublicProxy  # noqa: E402

SNAPSHOT = r"""prefix => {
 const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth&&getComputedStyle(e).visibility!=='hidden'};
 const nodes=[...document.querySelectorAll('a[href],button,input:not([type=hidden]),textarea,select,[role=button],[role=link],[contenteditable=true]')].filter(visible).slice(0,180);
 const elements=nodes.map((e,i)=>{const ref=prefix+'_'+i;e.setAttribute('data-dfy-ref',ref);return {ref,tag:e.tagName.toLowerCase(),type:e.getAttribute('type')||'',role:e.getAttribute('role')||'',label:(e.getAttribute('aria-label')||e.labels?.[0]?.innerText||e.innerText||e.placeholder||e.name||'').slice(0,150),href:e.tagName==='A'?e.href:undefined,options:e.tagName==='SELECT'?[...e.options].slice(0,30).map(o=>({value:o.value,label:o.text})):undefined}});
 return {title:document.title,text:(document.body?.innerText||'').slice(0,14000),elements};
}"""
VIDEO_LIST = r"""() => [...document.querySelectorAll('video')].map((v,i)=>({index:i,visible:(()=>{const r=v.getBoundingClientRect();return r.width>0&&r.height>0&&r.bottom>0&&r.top<innerHeight})(),paused:v.paused,time:v.currentTime,duration:Number.isFinite(v.duration)?v.duration:null,ready_state:v.readyState,muted:v.muted,width:v.videoWidth,height:v.videoHeight,rate:v.playbackRate,codecs:{webm:v.canPlayType('video/webm; codecs="vp8, vorbis"'),mp4:v.canPlayType('video/mp4; codecs="avc1.42E01E"')},error:v.error?.code||null}))"""
TRANSCRIPT = r"""v=>[...v.textTracks].map(t=>({language:t.language,label:t.label,active:[...(t.activeCues||[])].map(c=>c.text),cues:[...(t.cues||[])].slice(0,200).map(c=>({start:c.startTime,end:c.endTime,text:c.text}))}))"""


class Session:
    def __init__(self, context, root):
        self.context, self.root = context, root
        self.pages = {}
        self.current = None
        self.refs = {}
        self.revision = 0
        self.lock = asyncio.Lock()
        self.touched = time.monotonic()
        self.download_armed = False
        self.artifacts = {}
        self.blocked_requests = 0

    async def add_page(self, page):
        if page in self.pages.values():
            return
        if len(self.pages) >= 6:
            await page.close()
            return
        tid = "t" + uuid4().hex[:10]
        self.pages[tid] = page
        self.current = tid
        page.set_default_timeout(6000)
        page.set_default_navigation_timeout(25000)
        page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))

        async def download_guard(download):
            if not self.download_armed:
                await download.cancel()

        page.on(
            "download", lambda download: asyncio.create_task(download_guard(download))
        )

    def page(self, tid=None):
        tid = tid or self.current
        if tid not in self.pages or self.pages[tid].is_closed():
            raise BrowserError("标签已关闭，请先open或tabs")
        self.current = tid
        return self.pages[tid]


class BrowserWorker:
    def __init__(self, root, policy):
        self.root = Path(root).resolve()
        self.policy = policy
        self.sessions = {}
        self.creation_lock = asyncio.Lock()
        self.browser = None
        self.pw = None
        self.proxy = None
        self.download_root = self.root / "work/downloads"
        self.download_root.mkdir(parents=True, exist_ok=True)

    async def start(self):
        from playwright.async_api import async_playwright

        self.proxy = await PublicProxy(self.policy).start()
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            headless=True,
            executable_path=str(ROOT / "tools/browser/chrome/chrome.exe"),
            chromium_sandbox=True,
            ignore_default_args=["--mute-audio"],
            downloads_path=str(self.download_root),
            proxy={
                "server": f"http://127.0.0.1:{self.proxy.port}",
                "bypass": "<-loopback>",
            },
            args=[
                "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-background-networking",
            ],
        )

    async def session(self, key):
        async with self.creation_lock:
            return await self.create_session(key)

    async def create_session(self, key):
        if not re.fullmatch("[0-9a-f]{64}", key):
            raise BrowserError("会话标识无效")
        await self.prune()
        if key not in self.sessions:
            if len(self.sessions) >= 6:
                available = [
                    (k, v) for k, v in self.sessions.items() if not v.lock.locked()
                ]
                if not available:
                    raise BrowserError("浏览器当前繁忙，请稍后再试")
                old = min(available, key=lambda x: x[1].touched)[0]
                await self.close_session(old)
            context = await self.browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                accept_downloads=True,
                service_workers="block",
                permissions=[],
            )
            session = Session(context, self.root / "artifacts" / key)
            session.root.mkdir(parents=True, exist_ok=True)

            async def route(request_route):
                request = request_route.request
                try:
                    if request.url.startswith(("data:", "blob:")):
                        if request.is_navigation_request():
                            raise BrowserError("不允许data/blob顶层导航")
                    else:
                        self.policy.url(request.url)
                    if request.method not in {"GET", "HEAD", "OPTIONS"}:
                        from urllib.parse import urlsplit

                        u = urlsplit(request.url)
                        read_api = (
                            u.hostname == "youtube.com"
                            or (u.hostname or "").endswith(".youtube.com")
                        ) and re.fullmatch(
                            "/youtubei/v1/(player|browse|next|search)", u.path
                        )
                        if not read_api:
                            raise BrowserError("网站写入请求未开放")
                    await request_route.continue_()
                except Exception:
                    session.blocked_requests += 1
                    await request_route.abort()

            await context.route("**/*", route)

            async def block_socket(websocket):
                await websocket.close()

            await context.route_web_socket("**/*", block_socket)
            context.on("page", lambda page: asyncio.create_task(session.add_page(page)))
            self.sessions[key] = session
        session = self.sessions[key]
        session.touched = time.monotonic()
        return session

    async def close_session(self, key):
        session = self.sessions.pop(key, None)
        if session:
            await session.context.close()

    async def prune(self):
        for key, session in list(self.sessions.items()):
            if not session.lock.locked() and time.monotonic() - session.touched > 900:
                await self.close_session(key)
        artifact_root = self.root / "artifacts"
        if artifact_root.exists():
            for folder in artifact_root.iterdir():
                if folder.is_symlink() or not folder.is_dir():
                    continue
                for file in folder.iterdir():
                    if (
                        file.name != "bookmarks.json"
                        and file.is_file()
                        and not file.is_symlink()
                        and file.resolve().is_relative_to(artifact_root)
                        and time.time() - file.stat().st_mtime > 86400
                    ):
                        file.unlink()

    async def snapshot(self, session, page):
        session.current = next(
            (tid for tid, p in session.pages.items() if p is page), session.current
        )
        session.revision += 1
        session.refs = {}
        frames = []
        for index, frame in enumerate(page.frames[:10]):
            if frame.url not in {
                "about:blank",
                "about:srcdoc",
                "",
            } and not frame.url.startswith(("blob:", "data:")):
                try:
                    self.policy.url(frame.url)
                except BrowserError:
                    continue
            try:
                prefix = f"r{session.revision}f{index}"
                data = await frame.evaluate(SNAPSHOT, prefix)
                for element in data["elements"]:
                    session.refs[element["ref"]] = (frame, element)
                frames.append({"frame": index, **data})
            except Exception:
                continue
        return {
            "url": page.url,
            "title": await page.title(),
            "tab_id": session.current,
            "revision": session.revision,
            "frames": frames,
            "blocked_requests": session.blocked_requests,
            "untrusted_web_content": True,
        }

    async def element(self, session, ref):
        if ref not in session.refs:
            raise BrowserError("页面ref已失效，请重新snapshot后操作")
        frame, metadata = session.refs[ref]
        locator = frame.locator('[data-dfy-ref="' + ref + '"]')
        if await locator.count() != 1:
            raise BrowserError("页面元素已变化，请重新snapshot")
        return locator, metadata

    async def guard(self, element, metadata, action):
        label = metadata.get("label", "")
        field_type = (await element.get_attribute("type") or "").lower()
        if action in {"click", "download", "press_enter"}:
            href = await element.evaluate("e=>e.href||null")
            if href and not href.startswith("#"):
                self.policy.url(href)
        autocomplete = (await element.get_attribute("autocomplete") or "").lower()
        if field_type in {"password", "file"} or re.search(
            "password|cc-|one-time-code", autocomplete
        ):
            raise BrowserError("密码、验证码、支付或上传操作需要本人接管")
        if SENSITIVE_ACTION.search(label) and not re.search("cookie|饼干", label, re.I):
            raise BrowserError(
                "该按钮涉及账号或网站写入，需要本人另行操作；普通浏览仍可继续"
            )
        if action == "press_enter":
            allowed = await element.evaluate(
                "e=>e.type==='search'||e.form?.getAttribute('role')==='search'||/search|搜索/i.test([e.name,e.placeholder,e.getAttribute('aria-label'),e.form?.action].join(' '))"
            )
            if not allowed:
                raise BrowserError("只自动提交明确的站内搜索框，其他表单请本人处理")

    def artifact(self, session, raw, ext, kind):
        if len(raw) > 32 * 1024 * 1024:
            raise BrowserError("文件超过32MiB处理边界")
        token = "a" + uuid4().hex
        path = session.root / (token + ext)
        if path.parent.resolve() != session.root.resolve():
            raise BrowserError("文件目录越界")
        path.write_bytes(raw)
        item = {
            "id": token,
            "path": str(path),
            "kind": kind,
            "bytes": len(raw),
            "expires_at": time.time() + 86400,
        }
        session.artifacts[token] = item
        return item

    async def image(self, session, page, element=None):
        raw = await (element or page).screenshot(type="jpeg", quality=85, timeout=15000)
        item = self.artifact(session, raw, ".jpg", "image")
        return item, {
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(raw).decode()
            },
        }

    async def videos(self, page):
        videos = []
        for frame in page.frames[:10]:
            try:
                for item in await frame.evaluate(VIDEO_LIST):
                    videos.append((frame, item))
            except Exception:
                continue
        return sorted(videos, key=lambda item: not item[1].get("visible", True))

    async def execute(self, key, command):
        command = validate_command(command)
        action = command["action"]
        if action == "close_session":
            await self.close_session(key)
            return {"ok": True, "closed": True}
        session = await self.session(key)
        async with session.lock:
            if action == "status":
                return {
                    "ok": True,
                    "open_tabs": sum(not p.is_closed() for p in session.pages.values()),
                    "anonymous_context": True,
                    "idle_ttl_seconds": 900,
                }
            if action == "tabs":
                return {
                    "ok": True,
                    "tabs": [
                        {
                            "id": tid,
                            "url": page.url,
                            "title": await page.title(),
                            "current": tid == session.current,
                        }
                        for tid, page in list(session.pages.items())
                        if not page.is_closed()
                    ],
                }
            if action == "open":
                url = self.policy.url(command.get("url"))
                if not session.current or command.get("new_tab"):
                    if len(session.pages) >= 6:
                        raise BrowserError("当前会话最多6个标签，请先close_tab")
                    page = await session.context.new_page()
                    await session.add_page(page)
                else:
                    page = session.page()
                navigation_state = "domcontentloaded"
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                except Exception as error:
                    if (
                        type(error).__name__ != "TimeoutError"
                        or page.url in {"about:blank", ""}
                        or await page.locator("body").count() == 0
                    ):
                        raise
                    self.policy.url(page.url)
                    if not (
                        await page.locator("body").inner_text(timeout=3000)
                    ).strip():
                        raise
                    navigation_state = "partial_after_timeout"
                return {
                    "ok": True,
                    "navigation_state": navigation_state,
                    **await self.snapshot(session, page),
                }
            if action in {"bookmarks", "bookmark_remove"}:
                path = session.root / "bookmarks.json"
                items = (
                    json.loads(path.read_text(encoding="utf-8"))
                    if path.exists()
                    else []
                )
                if action == "bookmark_remove":
                    items = [x for x in items if x["url"] != command.get("url")]
                    path.write_text(
                        json.dumps(items, ensure_ascii=False), encoding="utf-8"
                    )
                return {"ok": True, "bookmarks": items}
            if action == "share":
                item = session.artifacts.get(command.get("artifact_id"))
                if (
                    not item
                    or not Path(item["path"]).is_file()
                    or item["expires_at"] < time.time()
                ):
                    raise BrowserError("文件不存在、已过期或不属于当前会话")
                return {"ok": True, "artifact": item, "share": True}
            page = session.page(command.get("tab_id"))
            if action == "close_tab":
                closed = session.current
                await page.close()
                session.pages.pop(closed, None)
                session.refs = {}
                session.current = next(iter(session.pages), None)
                return {"ok": True, "closed_tab": closed}
            if action == "switch_tab":
                return {"ok": True, **await self.snapshot(session, page)}
            if action == "read":
                index = int(command.get("frame", 0))
                if index >= len(page.frames):
                    raise BrowserError("页面frame不存在")
                offset = int(command.get("offset", 0))
                limit = int(command.get("limit", 10000))
                data = await page.frames[index].evaluate(
                    '([offset,limit])=>{const text=document.body?.innerText||"";return {text:text.slice(offset,offset+limit),total_characters:text.length,next_offset:offset+limit<text.length?offset+limit:null}}',
                    [offset, limit],
                )
                return {
                    "ok": True,
                    "url": page.url,
                    "frame": index,
                    "offset": offset,
                    **data,
                    "untrusted_web_content": True,
                }
            if action in {"snapshot", "find"}:
                data = await self.snapshot(session, page)
                if action == "find":
                    query = command.get("text", "")
                    data["matches"] = [
                        {
                            "frame": f["frame"],
                            "excerpt": f["text"][
                                max(0, f["text"].find(query) - 200) : f["text"].find(
                                    query
                                )
                                + 500
                            ],
                        }
                        for f in data["frames"]
                        if query and query in f["text"]
                    ]
                return {"ok": True, **data}
            if action == "bookmark_add":
                url = self.policy.url(page.url)
                path = session.root / "bookmarks.json"
                items = (
                    json.loads(path.read_text(encoding="utf-8"))
                    if path.exists()
                    else []
                )
                if url not in {x["url"] for x in items}:
                    items.append({"url": url, "title": (await page.title())[:200]})
                path.write_text(
                    json.dumps(items[-1000:], ensure_ascii=False), encoding="utf-8"
                )
                return {"ok": True, "bookmarked_url": url}
            if action in {"screenshot", "pdf"}:
                if action == "screenshot":
                    item, img = await self.image(session, page)
                    images = [img]
                else:
                    item = self.artifact(
                        session,
                        await page.pdf(format="A4", print_background=True),
                        ".pdf",
                        "file",
                    )
                    images = []
                return {
                    "ok": True,
                    "artifact": item,
                    "images": images,
                    "share": command.get("share", False),
                }
            if action.startswith("video_"):
                videos = await self.videos(page)
                if action == "video_list":
                    return {
                        "ok": True,
                        "videos": [
                            {
                                "index": i,
                                **{k: v for k, v in item.items() if k != "index"},
                            }
                            for i, (_, item) in enumerate(videos)
                        ],
                    }
                index = command.get("video", 0)
                if not isinstance(index, int) or index >= len(videos):
                    raise BrowserError(
                        "尚未发现可用video元素；可先滚动/播放或video_list"
                    )
                frame, meta = videos[index]
                video = frame.locator("video").nth(meta["index"])
                images = []
                notes = []
                if action in {"video_play", "video_observe"}:
                    try:
                        await video.evaluate("async v=>{v.muted=true;await v.play()}")
                    except Exception:
                        raise BrowserError(
                            "视频未能播放，可能尚未加载、需登录/验证码或站点限制"
                        ) from None
                elif action == "video_pause":
                    await video.evaluate("v=>v.pause()")
                elif action == "video_seek":
                    await video.evaluate(
                        "(v,t)=>{v.currentTime=t}", command.get("time", 0)
                    )
                elif action == "video_rate":
                    await video.evaluate(
                        "(v,r)=>{v.playbackRate=r}", command.get("rate", 1)
                    )
                elif action == "video_mute":
                    await video.evaluate(
                        "(v,m)=>{v.muted=m}", command.get("enabled", True)
                    )
                if action == "video_observe":
                    duration = command.get("seconds", 6)
                    for _ in range(3):
                        await asyncio.sleep(duration / 3)
                        item, img = await self.image(session, page, video)
                        images.append(img)
                        notes.append(
                            await video.evaluate(
                                "v=>({time:v.currentTime,width:v.videoWidth,height:v.videoHeight,paused:v.paused})"
                            )
                        )
                transcript = await video.evaluate(TRANSCRIPT)
                return {
                    "ok": True,
                    "video": (await frame.evaluate(VIDEO_LIST))[meta["index"]],
                    "transcript": transcript,
                    "observations": notes,
                    "images": images,
                    "coverage": "sampled_visible_frames_and_available_text_tracks_only_no_audio_transcription",
                    "untrusted_web_content": True,
                }
            if action in {
                "click",
                "type",
                "press",
                "select",
                "check",
                "hover",
                "drag",
                "download",
            }:
                element, meta = await self.element(session, command.get("ref"))
                await self.guard(
                    element,
                    meta,
                    "press_enter"
                    if action == "press" and command.get("key", "Enter") == "Enter"
                    else action,
                )
                if action == "click":
                    await element.click()
                elif action == "type":
                    await element.fill(command.get("text", ""))
                elif action == "press":
                    await element.press(
                        " "
                        if command.get("key") == "Space"
                        else command.get("key", "Enter")
                    )
                elif action == "select":
                    await element.select_option(command.get("values", []))
                elif action == "check":
                    await element.set_checked(command.get("enabled", True))
                elif action == "hover":
                    await element.hover()
                elif action == "drag":
                    target, _ = await self.element(session, command.get("target_ref"))
                    await element.drag_to(target)
                elif action == "download":
                    session.download_armed = True
                    try:
                        async with page.expect_download(timeout=25000) as info:
                            await element.click()
                        download = await info.value
                        ext = Path(download.suggested_filename).suffix.lower()
                        if ext not in ALLOWED_EXTENSIONS:
                            await download.cancel()
                            raise BrowserError(
                                "该下载类型未开放，不下载或运行可执行程序"
                            )
                        task = asyncio.create_task(download.path())
                        while not task.done():
                            if any(
                                f.is_file() and f.stat().st_size > 32 * 1024 * 1024
                                for f in self.download_root.iterdir()
                            ):
                                await download.cancel()
                                raise BrowserError("下载超过32MiB资源边界")
                            await asyncio.sleep(0.2)
                        path = await task
                        raw = Path(path).read_bytes()
                        item = self.artifact(
                            session,
                            raw,
                            ext,
                            "image"
                            if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
                            else "file",
                        )
                        await download.delete()
                        return {
                            "ok": True,
                            "artifact": item,
                            "share": command.get("share", False),
                        }
                    finally:
                        session.download_armed = False
            elif action == "scroll":
                direction = command.get("direction", "down")
                amount = command.get("amount", 600)
                if direction in {"top", "bottom"}:
                    await page.evaluate(
                        'd=>window.scrollTo(0,d==="top"?0:document.documentElement.scrollHeight)',
                        direction,
                    )
                else:
                    await page.mouse.wheel(
                        amount
                        if direction == "right"
                        else -amount
                        if direction == "left"
                        else 0,
                        -amount
                        if direction == "up"
                        else amount
                        if direction == "down"
                        else 0,
                    )
            elif action in {"back", "forward", "reload"}:
                await getattr(
                    page,
                    {"back": "go_back", "forward": "go_forward", "reload": "reload"}[
                        action
                    ],
                )(wait_until="domcontentloaded")
            elif action == "wait":
                await asyncio.sleep(min(3, command.get("seconds", 1)))
            else:
                raise BrowserError("操作未实现")
            return {"ok": True, **await self.snapshot(session, session.page())}

    async def close(self):
        for key in list(self.sessions):
            await self.close_session(key)
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()
        if self.proxy:
            await self.proxy.close()


async def serve(root, test_ports):
    worker = BrowserWorker(root, PublicPolicy(test_ports))
    tasks = {}
    housekeeping = None

    def emit(data):
        line = json.dumps(data, ensure_ascii=False, allow_nan=False)
        if len(line.encode()) > 8 * 1024 * 1024:
            line = json.dumps(
                {"id": data.get("id"), "error": "浏览器结果过大，请缩小范围"}
            )
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    async def execute(request):
        rid = request["id"]
        key = request["key"]
        try:
            result = await asyncio.wait_for(worker.execute(key, request["command"]), 50)
            emit({"id": rid, "result": result})
        except asyncio.CancelledError:
            await worker.close_session(key)
            emit({"id": rid, "error": "浏览器操作已取消"})
        except Exception as error:
            emit(
                {
                    "id": rid,
                    "error": str(error)
                    if isinstance(error, BrowserError)
                    else "浏览器操作未完成：" + type(error).__name__,
                }
            )
        finally:
            tasks.pop(rid, None)

    try:
        await worker.start()

        async def maintain():
            while True:
                await asyncio.sleep(30)
                await worker.prune()

        housekeeping = asyncio.create_task(maintain())
        emit({"ready": True, "version": worker.browser.version})
        while line := await asyncio.to_thread(sys.stdin.readline):
            if len(line) > 65536:
                emit({"error": "请求过大"})
                continue
            try:
                request = json.loads(line)
                if request.get("shutdown"):
                    break
                if request.get("cancel"):
                    task = tasks.get(request["cancel"])
                    if task:
                        task.cancel()
                    continue
                if (
                    not isinstance(request.get("id"), str)
                    or request["id"] in tasks
                    or not re.fullmatch("[0-9a-f]{64}", request.get("key", ""))
                ):
                    raise ValueError()
                tasks[request["id"]] = asyncio.create_task(execute(request))
            except (ValueError, TypeError):
                emit({"error": "无效浏览器请求"})
    finally:
        if housekeeping:
            housekeeping.cancel()
            await asyncio.gather(housekeeping, return_exceptions=True)
        for task in list(tasks.values()):
            task.cancel()
        await asyncio.gather(*list(tasks.values()), return_exceptions=True)
        await worker.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--test-port", type=int, action="append", default=[])
    args = parser.parse_args()
    asyncio.run(serve(Path(args.root), args.test_port))
